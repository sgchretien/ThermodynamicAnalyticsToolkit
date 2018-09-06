# This is heavily inspired by  https://github.com/openai/iaf/blob/master/tf_utils/adamax.py
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.framework import ops
import tensorflow as tf

from TATi.samplers.hamiltonianmontecarlosamplerfirstordersampler import HamiltonianMonteCarloSamplerFirstOrderSampler


class HamiltonianMonteCarloSamplerSecondOrderSampler(HamiltonianMonteCarloSamplerFirstOrderSampler):
    """ Implements a Hamiltonian Monte Carlo Sampler
    in the form of a TensorFlow Optimizer, overriding tensorflow.python.training.Optimizer.

    """
    def __init__(self,
                 ensemble_precondition, step_width, inverse_temperature,
                 loss, current_step, next_eval_step, hd_steps, accept_seed,
                 seed=None, use_locking=False, name='HamiltonianMonteCarlo_2ndOrder'):
        """ Init function for this class.

        :param ensemble_precondition: whether to precondition the gradient using
                all the other walkers or not
        :param step_width: step width for gradient
        :param inverse_temperature: scale for noise
        :param loss: loss value of the current state for evaluating acceptance
        :param current_step: current step
        :param next_eval_step: step number at which accept/reject is evaluated next
        :param seed: seed value of the random number generator for generating reproducible runs
        :param use_locking: whether to lock in the context of multi-threaded operations
        :param name: internal name of optimizer
        """
        super(HamiltonianMonteCarloSamplerSecondOrderSampler, self).__init__(
            ensemble_precondition, step_width, inverse_temperature,
            loss, current_step, next_eval_step, accept_seed,
            seed, use_locking, name)
        self._hd_steps = hd_steps

    def _prepare(self):
        """ Converts step width into a tensor, if given as a floating-point
        number.
        """
        super(HamiltonianMonteCarloSamplerSecondOrderSampler, self)._prepare()
        self._hd_steps_t = ops.convert_to_tensor(self._hd_steps, name="hd_steps")


    def _apply_dense(self, grads_and_vars, var):
        """ Adds nodes to TensorFlow's computational graph in the case of densely
        occupied tensors to perform the actual sampling.

        We perform a number of Leapfrog steps on a hamiltonian (loss+kinetic energy)
        and at step number next_eval_step we check the acceptance criterion,
        either resetting back to the initial parameters or resetting the
        initial parameters to the current ones.

        NOTE:
            Due to Tensorflow enforcing loss and gradient evaluation at
            the begin of the sampling step, we need to cyclically permute the
            BAB steps to become BBA, i.e. the last "B" step is delayed till the
            next step. This means that we need to skip the additional "B" step
            in the very first time integration step and we need an additional
            step to compute the delayed "B" for the last time integration and
            subsequently to compute the kinetic energy before the criterion
            evaluation.

            Effectively, we compute L+2 steps if L is the number of Hamiltonian
            dynamics steps.

        :param grads_and_vars: gradient nodes over all walkers and all variables
        :param var: parameters of the neural network
        :return: a group of operations to be added to the graph
        """
        grad = self._pick_grad(grads_and_vars, var)
        step_width_t, inverse_temperature_t, current_step_t, next_eval_step_t, random_noise_t, uniform_random_t = \
            self._prepare_dense(grad, var)
        hd_steps_t =  math_ops.cast(self._hd_steps_t, tf.int64)

        momentum = self.get_slot(var, "momentum")
        initial_parameters = self.get_slot(var, "initial_parameters")

        # \nabla V (q^n ) \Delta t
        scaled_gradient = step_width_t * grad

        gradient_global_t = self._add_gradient_contribution(scaled_gradient)
        virial_global_t = self._add_virial_contribution(grad, var)

        # update momentum: B, BB or redraw momenta
        scaled_noise = tf.sqrt(1./inverse_temperature_t)*random_noise_t
        momentum_criterion_block_t = self._get_momentum_criterion_block(
            scaled_noise, momentum, scaled_gradient, current_step_t, next_eval_step_t)

        momentum_sq = tf.reduce_sum(tf.multiply(momentum_criterion_block_t, momentum_criterion_block_t))
        momentum_global_t = self._add_momentum_contribution(momentum_sq)
        kinetic_energy_t = self._add_kinetic_energy_contribution(momentum_sq)
        current_energy = self._get_current_total_energy()

        # prior force act directly on var
        #ub_repell, lb_repell = self._apply_prior(var)
        #prior_force = step_width_t * (ub_repell + lb_repell)

        #scaled_momentum = step_width_t * momentum_criterion_block_t - prior_force

        # update variables: A, skip or evaluate criterion (accept/reject)
        scaled_momentum = step_width_t * momentum_criterion_block_t
        p_accept = self._create_p_accept(inverse_temperature_t, current_energy)
        criterion_block_t = self._create_criterion_integration_block(
            virial_global_t, scaled_momentum, current_energy,
            initial_parameters, var,
            p_accept, uniform_random_t,
            current_step_t, next_eval_step_t
        )

        # note: these are evaluated in any order, use control_dependencies if required
        return control_flow_ops.group(*([momentum_criterion_block_t, criterion_block_t,
                                        virial_global_t, gradient_global_t,
                                        momentum_global_t, kinetic_energy_t]))

    def _apply_sparse(self, grad, var):
        """ Adds nodes to TensorFlow's computational graph in the case of sparsely
        occupied tensors to perform the actual sampling.

        Note that this is not implemented so far.

        :param grad: gradient nodes, i.e. they contain the gradient per parameter in `var`
        :param var: parameters of the neural network
        :return: a group of operations to be added to the graph
        """
        raise NotImplementedError("Sparse gradient updates are not supported.")
