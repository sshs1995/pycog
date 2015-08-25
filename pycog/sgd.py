"""
Train a recurrent neural network using minibatch stochastic gradient descent 
with the modifications described in

  On the difficulty of training recurrent neural networks.
  R. Pascanu, T. Mikolov, & Y. Bengio, ICML 2013.

  https://github.com/pascanur/trainingRNNs

"""
from __future__ import absolute_import
from __future__ import division

import cPickle as pickle
import datetime
import os
import sys

import numpy as np

import theano
import theano.tensor as T

from .      import theanotools
from .rnn   import RNN
from .utils import dump

this = 'pycog.sgd'

class SGD(object):
    """
    Stochastic gradient descent training for RNNs.

    """
    @staticmethod
    def clip_norm(v, norm, maxnorm):
        """ 
        Renormalize the vector v if its norm exceeds maxnorm.

        """
        return T.switch(norm > maxnorm, maxnorm*v/norm, v)

    def __init__(self, trainables, inputs, costs, regs, x, z, params, save_values, 
                 extras):
        """
        Construct the necessary Theano functions.

        Parameters
        ----------

        trainables : list
                     List of Theano variables to optimize.

        inputs : [inputs, targets]
                 Dataset used to train the RNN.

        costs : [loss, ...]
                costs[0] is the loss that is optimized. costs[1:] are used for
                monitoring only.

        regs : theano.tensor.var.TensorVariable
               Regularization terms to add to costs[0].

        x : Theano variable
            Hidden unit activities.

        z : Theano variable
            Outputs.
        
        params : dict
                 All parameters associated with the training of this network -- this
                 will be saved as part of the RNN savefile.

        save_values : list
                      List of Theano variables to save.

        extras : dict
                 Additinal information needed by the SGD training algorithm 
                 (specifically, for computing the regularization term) that may not 
                 be needed by other training algorithms (e.g., Hessian-free).
        
        """
        self.trainables  = trainables
        self.p           = params
        self.save_values = save_values

        self.trainable_names = [tr.name for tr in trainables]

        #---------------------------------------------------------------------------------
        # Setup
        #---------------------------------------------------------------------------------

        lambda_Omega = T.scalar('lambda_Omega')
        lr           = T.scalar('lr')
        maxnorm      = T.scalar('maxnorm')
        bound        = T.scalar('bound')

        #---------------------------------------------------------------------------------
        # Compute gradient
        #---------------------------------------------------------------------------------

        # Pascanu's trick for getting dL/dxt
        scan_node = x.owner.inputs[0].owner
        assert isinstance(scan_node.op, theano.scan_module.scan_op.Scan)
        npos   = scan_node.op.n_seqs + 1
        init_x = scan_node.inputs[npos]
        g_x,   = theanotools.grad(costs[0], [init_x])

        # Get into "standard" order
        Win, Wrec, Wout, brec, bout, x0 = RNN.fill(self.trainables, self.trainable_names)

        # Gradients
        g = theanotools.grad(costs[0] + regs, self.trainables)
        g_Win, g_Wrec, g_Wout, g_brec, g_bout, g_x0 = RNN.fill(g, self.trainable_names)

        #---------------------------------------------------------------------------------
        # For vanishing gradient regularizer
        #---------------------------------------------------------------------------------

        self.Wrec_ = extras['Wrec_']
        d_f_hidden = extras['d_f_hidden']

        #---------------------------------------------------------------------------------
        # Regularization for the vanishing gradient problem
        #---------------------------------------------------------------------------------

        if np.isscalar(self.p['tau']):
            alpha = T.scalar('alpha')
        else:
            alpha = T.vector('alpha')
        d_xt = T.tensor3('d_xt')
        xt   = T.tensor3('xt')

        num    = ((1 - alpha)*d_xt[1:] + alpha*T.dot(d_xt[1:], self.Wrec_)*d_f_hidden(xt))
        num    = (num**2).sum(axis=2)
        denom  = (d_xt[1:]**2).sum(axis=2)
        Omega  = (T.switch(T.ge(denom, bound), num/denom, 1) - 1)**2
        nelems = T.mean(T.ge(denom, bound), axis=1)
        Omega  = Omega.mean(axis=1).sum()/nelems.sum()

        tmp_g_Wrec = theanotools.grad(Omega, Wrec)
        Omega, tmp_g_Wrec, nelems = theano.clone(
            [Omega, tmp_g_Wrec, nelems.mean()], replace=[(d_xt, g_x), (xt, x)]
            )

        g_Wrec += lambda_Omega * tmp_g_Wrec

        #---------------------------------------------------------------------------------
        # Gradient clipping
        #---------------------------------------------------------------------------------

        g = []
        if 'Win' in self.trainable_names:
            g += [g_Win]
        g += [g_Wrec, g_Wout]
        if 'brec' in self.trainable_names:
            g += [g_brec]
        if 'bout' in self.trainable_names:
            g += [g_bout]
        if 'x0' in self.trainable_names:
            g += [g_x0]

        gnorm = T.sqrt(sum([(i**2).sum() for i in g]))
        g = [SGD.clip_norm(i, gnorm, maxnorm) for i in g]
        g_Win, g_Wrec, g_Wout, g_brec, g_bout, g_x0 = RNN.fill(g, self.trainable_names)

        # Pascanu's safeguard for numerical precision issues with float32
        new_cond = T.or_(T.or_(T.isnan(gnorm), T.isinf(gnorm)),
                         T.or_(gnorm < 0, gnorm > 1e10))
        if 'Win' in self.trainable_names:
            g_Win  = T.switch(new_cond, np.float32(0), g_Win)
        g_Wrec = T.switch(new_cond, np.float32(0.02)*Wrec, g_Wrec)
        g_Wout = T.switch(new_cond, np.float32(0), g_Wout)
        if 'brec' in self.trainable_names:
            g_brec = T.switch(new_cond, np.float32(0), g_brec)
        if 'bout' in self.trainable_names:
            g_bout = T.switch(new_cond, np.float32(0), g_bout)
        if 'x0' in self.trainable_names:
            g_x0 = T.switch(new_cond, np.float32(0), g_x0)

        #---------------------------------------------------------------------------------
        # Training step
        #---------------------------------------------------------------------------------

        # Final gradients
        g = []
        if 'Win' in self.trainable_names:
            g += [g_Win]
        g += [g_Wrec, g_Wout]
        if 'brec' in self.trainable_names:
            g += [g_brec]
        if 'bout' in self.trainable_names:
            g += [g_bout]
        if 'x0' in self.trainable_names:
            g += [g_x0]

        # Update rule
        updates = [(theta, theta - lr*grad) for theta, grad in zip(self.trainables, g)]

        # Update function
        self.train_step = theanotools.function(
            inputs + [alpha, lambda_Omega, lr, maxnorm, bound],
            [costs[0] + regs, gnorm, Omega, nelems, x],
            updates=updates
            )

        # Cost function
        self.f_cost = theanotools.function(inputs, [costs[0] + regs] + costs[1:] + [z])

    #/////////////////////////////////////////////////////////////////////////////////////

    @staticmethod
    def get_value(x):
        if hasattr(x, 'get_value'):
            return x.get_value(borrow=True)
        if hasattr(x, 'eval'):
            return x.eval()
        return x

    @staticmethod
    def get_values(vars):
        """
        Get the parameters to save.

        """
        return [SGD.get_value(i) for i in vars]

    #/////////////////////////////////////////////////////////////////////////////////////

    def train(self, gradient_data, validation_data, savefile):
        """
        Train the RNN.
        
        Paramters
        ---------

        gradient_data : pycog.Dataset
                        Gradient dataset.

        validation_data : pycog.Dataset
                          Validation dataset.

        savefile : str
                   File to save network information in.

        """
        checkfreq = self.p['checkfreq']
        if checkfreq is None:
            checkfreq = int(5e3)//gradient_data.minibatch_size

        patience = self.p['patience']
        if patience is None:
            patience = 100*checkfreq

        alpha        = self.p['dt']/self.p['tau']
        lambda_Omega = self.p['lambda_Omega']
        lr           = self.p['learning_rate']
        maxnorm      = self.p['max_gradient_norm']
        bound        = self.p['bound']
        save_exclude = ['performance', 'terminate']

        #---------------------------------------------------------------------------------
        # Continue previous run if we can
        #---------------------------------------------------------------------------------

        if os.path.isfile(savefile):
            with open(savefile) as f:
                save = pickle.load(f)
            best          = save['best']
            init_p        = save['current']
            first_iter    = save['iter']
            costs_history = save['costs_history']
            Omega_history = save['Omega_history']

            # Restore RNGs for datasets
            gradient_data.rng   = save['rng_gradient']
            validation_data.rng = save['rng_validation']

            # Restore parameter values
            for i, j in zip(self.trainables, init_p):
                i.set_value(j)

            print("[ SGD.train ] Recovered saved model,"
                  " continuing from iteration {}.".format(first_iter))
        else:
            best = {
                'iter':        1,
                'cost':        np.inf,
                'other_costs': [],
                'params':      SGD.get_values(self.save_values)
                }
            first_iter    = best['iter']
            costs_history = []
            Omega_history = []

            # Save initial conditions
            save = {
                'params':         {k: v for k, v in self.p.items() 
                                   if k not in save_exclude},
                'varlist':        self.trainable_names,
                'iter':           1,
                'current':        SGD.get_values(self.trainables),
                'best':           best,
                'costs_history':  costs_history,
                'Omega_history':  Omega_history,
                'rng_gradient':   gradient_data.rng,
                'rng_validation': validation_data.rng
                }
            base, ext = os.path.splitext(savefile)
            dump(base + '_init' + ext, save)

        #---------------------------------------------------------------------------------
        # Updates
        #---------------------------------------------------------------------------------

        performance = self.p['performance']
        terminate   = self.p['terminate']
        tr_Omega    = None
        tr_gnorm    = None
        try:
            tstart = datetime.datetime.now()
            for iter in xrange(first_iter, 1+self.p['max_iter']):
                if iter % checkfreq == 1:
                    #---------------------------------------------------------------------
                    # Timestamp
                    #---------------------------------------------------------------------

                    tnow      = datetime.datetime.now()
                    totalsecs = (tnow - tstart).total_seconds()

                    hrs  = int(totalsecs//3600)
                    mins = int(totalsecs%3600)//60
                    secs = int(totalsecs%60)

                    timestamp = tnow.strftime('%b %d %Y %I:%M:%S %p').replace(' 0', ' ')
                    print('{} updates - {} ({} hrs {} mins {} secs elapsed)'
                          .format(iter-1, timestamp, hrs, mins, secs))

                    #---------------------------------------------------------------------
                    # Validate
                    #---------------------------------------------------------------------

                    # Validation cost
                    costs = self.f_cost(*validation_data(best['other_costs']))
                    z     = costs[-1]
                    costs = [float(i) for i in costs[:-1]]
                    s     = "| validation: {:.8f} / {:.8f}".format(costs[0], costs[1])

                    # Dashes
                    nfill = 70

                    # Compute task-specific performance
                    if performance is not None:
                        costs.append(performance(validation_data.get_trials(),
                                                 SGD.get_value(z)))
                        sp     = " / {:.8f}".format(costs[-1])
                        s     += sp
                        nfill -= len(sp)

                    # Keep track of costs
                    costs_history.append((gradient_data.ntrials, costs))

                    # Record the value of the regularization term in the last iteration
                    if tr_Omega is not None:
                        Omega_history.append(
                            (gradient_data.ntrials, lambda_Omega*tr_Omega)
                            )

                    # New best
                    if costs[0] < best['cost']:
                        s += ' ' + '-'*(nfill - len(s))
                        s += " NEW BEST (prev. best: {:.8f})".format(best['cost'])
                        best = {
                            'iter':        iter,
                            'cost':        costs[0],
                            'other_costs': costs[1:],
                            'params':      SGD.get_values(self.save_values)
                            }
                    print(s)

                    # Spectral radius
                    rho = RNN.spectral_radius(self.Wrec_.eval())

                    # Format
                    Omega = ('n/a' if tr_Omega is None 
                             else '{:.8f}'.format(float(tr_Omega)))
                    gnorm = ('n/a' if tr_gnorm is None 
                             else '{:.8f}'.format(float(tr_gnorm)))
                    
                    # Info
                    print("| Omega      (last iter) = {}".format(Omega))
                    print("| grad. norm (last iter) = {}".format(gnorm))
                    print("| rho                    = {:.8f}".format(rho))
                    sys.stdout.flush()

                    #---------------------------------------------------------------------
                    # Save progress
                    #---------------------------------------------------------------------

                    save = {
                        'params':         {k: v for k, v in self.p.items()
                                           if k not in save_exclude},
                        'varlist':        self.trainable_names,
                        'iter':           iter,
                        'current':        SGD.get_values(self.trainables),
                        'best':           best,
                        'costs_history':  costs_history,
                        'Omega_history':  Omega_history,
                        'rng_gradient':   gradient_data.rng,
                        'rng_validation': validation_data.rng
                        }
                    dump(savefile, save)

                    if costs[1] <= self.p['min_error']:
                        print("Reached minimum error of {:.6f}"
                              .format(self.p['min_error']))
                        break

                    if terminate(np.array([c[-1] for _, c in costs_history])):
                        print("Termination criterion satisfied -- we\'ll call it a day.")
                        break

                if iter - best['iter'] > patience:
                    print("We've run out of patience -- time to give up.")
                    break

                #-------------------------------------------------------------------------
                # Training step
                #-------------------------------------------------------------------------

                tr_cost, tr_gnorm, tr_Omega, tr_nelems, tr_x = self.train_step(
                    *(gradient_data(best['other_costs'])
                      + [alpha, lambda_Omega, lr, maxnorm, bound])
                     )

                #-------------------------------------------------------------------------
        except KeyboardInterrupt:
            print("Training interrupted by user during iteration {}.".format(iter))
