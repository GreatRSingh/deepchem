import torch
import functools
from typing import Optional


def rk_step(func, t, y, f, h, abck):
    """Perform a single step of the Runge-Kutta method.

    Parameters
    ----------
    func: callable
        Function to be integrated. It should produce output of list of
        tensors following the shapes of tuple `y`. `t` should be a single element.
    t: torch.Tensor
        Integrated times
    y: List[torch.Tensor]
        List of initial values
    f: List[torch.Tensor]
        List of derivative values
    h: torch.Tensor
        The step size
    abck: Tuple[torch.Tensor]
        Tuple of A, B, C, K. A is the Runge-Kutta matrix, B is the weights,
        C is the nodes, and K is the intermediate values.
        A: (norder, norder)
        B: (norder,)
        C: (norder,)
        K: (norder+1, ny)

    Returns
    -------
    ynew: List[torch.Tensor]
        New values of the states
    fnew: List[torch.Tensor]
        New values of the derivatives

    """

    A, B, C, K = abck
    K[0] = f
    for s, (a, c) in enumerate(zip(A[1:], C[1:]), start=1):
        dy = torch.matmul(K[:s].T, a[:s]) * h
        K[s] = func(t + c * h, y + dy)
    ynew = y + h * torch.matmul(K[:-1].T, B)
    fnew = func(t + h, ynew)
    K[-1] = fnew
    return ynew, fnew

class RKAdaptiveStepSolver(object):
    """Runge-Kutta adaptive step solver."""
    A: Optional[torch.Tensor] = None
    B: Optional[torch.Tensor] = None
    C: Optional[torch.Tensor] = None
    E: Optional[torch.Tensor] = None
    n_stages: Optional[int] = None
    error_estimator_order: Optional[int] = None

    def __init__(self, atol, rtol):
        """Initialize the solver.

        Parameters
        ----------
        atol: float
            The absolute error tolerance in deciding the steps
        rtol: float
            The relative error tolerance in deciding the steps

        """
        self.atol = atol
        self.rtol = rtol
        self.max_factor = 10
        self.min_factor = 0.2
        self.step_mult = 0.9
        self.error_exponent = -1. / (self.error_estimator_order + 1.)

    def setup(self, fcn, ts, y0, params):
        """Setup the solver.

        Parameters
        ----------
        fcn: callable
            Function to be integrated. It should produce output of list of
            tensors following the shapes of tuple `y`. `t` should be a single element.
        ts: torch.Tensor
            The integrated times
        y0: torch.Tensor
            The initial values
        params: list
            List of any other parameters

        """
        # flatten the y0, will be restore at the end of .solve()
        self.yshape = y0.shape
        self.y0 = y0.reshape(-1)

        direction = ts[1] - ts[0]
        if direction < 0:
            self.ts = -ts
            self.func = lambda t, y: -fcn(-t, y.reshape(self.yshape), *params).reshape(-1)
        else:
            self.ts = ts
            self.func = lambda t, y: fcn(t, y.reshape(self.yshape), *params).reshape(-1)
        self.dtype = y0.dtype
        self.device = y0.device
        n = torch.numel(y0)
        self.K = torch.empty((self.n_stages + 1, n), dtype=self.dtype, device=self.device)

        # convert the predefined tensors into the dtype and device
        self.A = self.A.to(self.dtype).to(self.device)
        self.B = self.B.to(self.dtype).to(self.device)
        self.C = self.C.to(self.dtype).to(self.device)
        self.E = self.E.to(self.dtype).to(self.device)

    def solve(self):
        """Solve the ODE.

        Returns
        -------
        yt: torch.Tensor
            The integrated values

        """
        t0 = self.ts[0]
        ts = self.ts
        f0 = self.func(t0, self.y0)
        h0 = self.ts[1] - self.ts[0]  # ??? perform more intelligent guess

        # prepare the results
        nt = len(ts)
        yt = torch.empty((len(self.ts), *self.y0.shape), dtype=self.dtype, device=self.device)
        yt[0] = self.y0

        rk_state = (f0, t0, self.y0, h0)
        for i in range(1, len(ts)):
            rk_state = self._step(rk_state, ts[i])
            yt[i] = rk_state[2]
        return yt.reshape(-1, *self.yshape)

    def _error_norm(self, K, h):
        """Calculate the error norm.

        Parameters
        ----------
        K: torch.Tensor
            The intermediate values
        h: torch.Tensor
            The step size

        Returns
        -------
        err: torch.Tensor
            The error norm

        """
        err = torch.matmul(K.T, self.E) * h
        return err.norm()

    def _step(self, rk_state, t1):
        """Perform a single step of the Runge-Kutta method.

        Parameters
        ----------
        rk_state: Tuple[torch.Tensor]
            The current state of the Runge-Kutta method
        t1: torch.Tensor
            The target time

        Returns
        -------
        rk_state: Tuple[torch.Tensor]
            The new state of the Runge-Kutta method

        """
        t1_achieved = False
        while not t1_achieved:
            rk_state, t1_achieved = self._single_step(rk_state, t1)
        return rk_state

    def _single_step(self, rk_state, t1):
        """Perform a single step of the Runge-Kutta method.

        Parameters
        ----------
        rk_state: Tuple[torch.Tensor]
            The current state of the Runge-Kutta method
        t1: torch.Tensor
            The target time

        Returns
        -------
        rk_state: Tuple[torch.Tensor]
            The new state of the Runge-Kutta method
        t1_achieved: bool
            Whether the target time is achieved

        """
        f0, t0, y0, h = rk_state
        accepted = False
        prev_rejected = False
        while not accepted:
            # check if the current step exceeds the target
            t1_achieved = t0 + h > t1
            hstep = t1 - t0 if t1_achieved else h
            tnew = t0 + hstep

            # perform the RK-step to t0+h
            abck = (self.A, self.B, self.C, self.K)
            ynew, fnew = rk_step(self.func, t0, y0, f0, hstep, abck)

            # estimate the error norm
            scale = self.atol + torch.max(y0.norm(), ynew.norm()) * self.rtol
            errnorm = self._error_norm(self.K, hstep) / scale
            accepted = errnorm < 1

            # adjust the step size
            if accepted and not t1_achieved:
                if errnorm == 0:
                    factor = self.max_factor
                else:
                    factor = min(self.max_factor, self.step_mult * errnorm ** self.error_exponent)

                if prev_rejected:
                    factor = min(1.0, factor)

                h *= factor
            elif not accepted:
                factor = max(self.min_factor, self.step_mult * errnorm ** self.error_exponent)
                h = hstep * factor

            prev_rejected = not accepted

        rk_state = (fnew, tnew, ynew, h)
        return rk_state, t1_achieved

class RK23(RKAdaptiveStepSolver):
    """Runge-Kutta method with order 2 and 3.

    Examples
    --------
    >>> import torch
    >>> def fcn(t, y):
    ...     return t + y
    >>> ts = torch.linspace(0, 1, 11)
    >>> y0 = torch.tensor([0.0])
    >>> params = []
    >>> solver = RK23(atol=1e-8, rtol=1e-5)
    >>> solver.setup(fcn, ts, y0, params)
    >>> solver.solve()
    tensor([[0.0000],
            [0.0052],
            [0.0214],
            [0.0499],
            [0.0918],
            [0.1487],
            [0.2221],
            [0.3138],
            [0.4255],
            [0.5596],
            [0.7183]])

    """
    error_estimator_order = 2
    n_stages = 3
    C = torch.tensor([0, 1 / 2, 3 / 4], dtype=torch.float64)
    A = torch.tensor([
        [0, 0, 0],
        [1 / 2, 0, 0],
        [0, 3 / 4, 0]
    ], dtype=torch.float64)
    B = torch.tensor([2 / 9, 1 / 3, 4 / 9], dtype=torch.float64)
    E = torch.tensor([5 / 72, -1 / 12, -1 / 9, 1 / 8], dtype=torch.float64)

class RK45(RKAdaptiveStepSolver):
    """Runge-Kutta method with order 4 and 5.
    
    Examples
    --------
    >>> import torch
    >>> def fcn(t, y):
    ...     return t + y
    >>> ts = torch.linspace(0, 1, 11)
    >>> y0 = torch.tensor([0.0])
    >>> params = []
    >>> solver = RK45(atol=1e-8, rtol=1e-5)
    >>> solver.setup(fcn, ts, y0, params)
    >>> solver.solve()
    tensor([[0.0000],
            [0.0052],
            [0.0214],
            [0.0499],
            [0.0918],
            [0.1487],
            [0.2221],
            [0.3138],
            [0.4255],
            [0.5596],
            [0.7183]])
    """
    error_estimator_order = 4
    n_stages = 6
    C = torch.tensor([0, 1 / 5, 3 / 10, 4 / 5, 8 / 9, 1], dtype=torch.float64)
    A = torch.tensor([
        [0, 0, 0, 0, 0],
        [1 / 5, 0, 0, 0, 0],
        [3 / 40, 9 / 40, 0, 0, 0],
        [44 / 45, -56 / 15, 32 / 9, 0, 0],
        [19372 / 6561, -25360 / 2187, 64448 / 6561, -212 / 729, 0],
        [9017 / 3168, -355 / 33, 46732 / 5247, 49 / 176, -5103 / 18656]
    ], dtype=torch.float64)
    B = torch.tensor([35 / 384, 0, 500 / 1113, 125 / 192, -2187 / 6784, 11 / 84], dtype=torch.float64)
    E = torch.tensor([-71 / 57600, 0, 71 / 16695, -71 / 1920, 17253 / 339200, -22 / 525,
                      1 / 40], dtype=torch.float64)

def _rk_adaptive(fcn, ts, y0, params, cls, atol=1e-8, rtol=1e-5, **unused):
    """Perform the adaptive Runge-Kutta steps.

    Examples
    --------
    >>> import torch
    >>> def fcn(t, y):
    ...     return -y
    >>> ts = torch.linspace(0, 1, 11)
    >>> y0 = torch.tensor([1.0])
    >>> params = []
    >>> yt = _rk_adaptive(fcn, ts, y0, params, RK45)
    >>> yt
    tensor([[1.0000],
            [0.9048],
            [0.8187],
            [0.7408],
            [0.6703],
            [0.6065],
            [0.5488],
            [0.4966],
            [0.4493],
            [0.4066],
            [0.3679]])

    Parameters
    ----------
    fcn: callable
        Function to be integrated. It should produce output of list of
        tensors following the shapes of tuple `y`. `t` should be a single element.
    ts: torch.Tensor
        The integrated times
    y0: torch.Tensor
        The initial values
    params: list
        List of any other parameters
    cls: RKAdaptiveStepSolver
        The class of the solver
    atol: float
        The absolute error tolerance in deciding the steps
    rtol: float
        The relative error tolerance in deciding the steps
    """
    solver = cls(atol=atol, rtol=rtol)
    solver.setup(fcn, ts, y0, params)
    return solver.solve()

@functools.wraps(_rk_adaptive, assigned='__annotations__')
def rk23_adaptive(fcn, ts, y0, params, **kwargs):
    """
    Perform the adaptive Runge-Kutta steps with order 2 and 3.

    Examples
    --------
    >>> import torch
    >>> def fcn(t, y):
    ...     return -y
    >>> ts = torch.linspace(0, 1, 11)
    >>> y0 = torch.tensor([1.0])
    >>> params = []
    >>> yt = rk45_adaptive(fcn, ts, y0, params)
    >>> yt
    tensor([[1.0000],
            [0.9048],
            [0.8187],
            [0.7408],
            [0.6703],
            [0.6065],
            [0.5488],
            [0.4966],
            [0.4493],
            [0.4066],
            [0.3679]])
    """
    return _rk_adaptive(fcn, ts, y0, params, RK23, **kwargs)

@functools.wraps(_rk_adaptive, assigned='__annotations__')
def rk45_adaptive(fcn, ts, y0, params, **kwargs):
    """
    Perform the adaptive Runge-Kutta steps with order 4 and 5.

    Examples
    --------
    >>> import torch
    >>> def fcn(t, y):
    ...     return -y
    >>> ts = torch.linspace(0, 1, 11)
    >>> y0 = torch.tensor([1.0])
    >>> params = []
    >>> yt = rk45_adaptive(fcn, ts, y0, params)
    >>> yt
    tensor([[1.0000],
            [0.9048],
            [0.8187],
            [0.7408],
            [0.6703],
            [0.6065],
            [0.5488],
            [0.4966],
            [0.4493],
            [0.4066],
            [0.3679]])

    """
    return _rk_adaptive(fcn, ts, y0, params, RK45, **kwargs)
