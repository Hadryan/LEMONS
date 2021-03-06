from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

import numpy as np
import sacred
import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter


class Logger(ABC):
    """ Extracts and/or persists tracker information. """

    def __init__(self, path: str = None):
        """
        Parameters
        ----------
        path : str or Path, optional
            Path to where data will be logged.
        """
        path = Path() if path is None else Path(path)
        self.path = path.expanduser().absolute()

    @abstractmethod
    def log_scalar(self, tag: str, value: float, step: int) -> None:
        """
        Log a scalar

        Parameters
        ----------
        tag : str
            Name of the parameter.
        value: float
            Scalar to log.
        step:
            Position in the log
        """
        pass


class SacredLogger(Logger):
    """ Log values to sacred. """

    def __init__(self, ex: sacred.Experiment):
        """
        Parameters
        ----------
        ex : sacred.Experiment
            Sacred Experiment for logging results
        """
        super().__init__()
        self.ex = ex

    def log_scalar(self, tag: str, value: float, step: int) -> None:
        self.ex.log_scalar(tag, value, step)


class TensorBoardLogger(Logger):
    """ Log values to tensorboard. """

    DEFAULT_DIR = "runs"

    def __init__(self, path: str = None):
        """
        Parameters
        ----------
        path : str or Path
            Path to the log directory.
        """
        super().__init__(TensorBoardLogger.DEFAULT_DIR if path is None else path)
        self.writer = SummaryWriter(self.path)

    def log_scalar(self, tag: str, value: float, step: int) -> None:
        self.writer.add_scalar(tag, value, step)


class Tracker:
    """ Tracks useful information on the current epoch. """

    def __init__(self, *loggers: Logger, log_every: int = 1, metrics: List[callable] = None,
                 metrics_names: List[str] = None, pre_tag: str = ''):
        """
        Parameters
        ----------
        logger0, logger1, ... loggerN : Logger
            One or more loggers for logging recsys information.
        log_every : int, optional
            Frequency of logging mini-batch results.
        metrics : List[callable] , optional
            List of metric functions to compute on (ground_truth, predictions).
        metrics_names: List[str], optional
            List of metric names for logging the above specified metrics.
        pre_tag: str, optional
            String to add at the beginning of the name of every logged value.
        """

        self.epoch = 0
        self.update = 0
        self.losses = []

        self.loggers = list(loggers)
        self.log_every = log_every

        self.metrics = metrics
        self.metrics_names = metrics_names
        self.pre_tag = pre_tag

    def _pre(self, string: str):
        return self.pre_tag + '.' + string if self.pre_tag else string

    def track_loss(self, loss: float):
        # 0th epoch is to be used for evaluating baseline performance
        if self.epoch > 0:
            self.update += 1

        if self.log_every > 0 and self.update % self.log_every == 0 and self.update != 0:
            for logger in self.loggers:
                logger.log_scalar(self._pre('loss'), loss, self.update)

        self.losses.append(loss)

    def summarise(self):
        """
        Returns: average of the losses in the epoch
        -------

        """
        avg_loss = float(np.mean(self.losses).item())
        self.losses.clear()

        for logger in self.loggers:
            logger.log_scalar(self._pre('avg_loss'), avg_loss, self.epoch)

        self.epoch += 1
        return avg_loss

    def compute_metrics(self, y_array: List[float], pred_array: List[float]):
        """
        Computes the metrics specified in the Tracker.

        Parameters
        ----------
        y_array: List[float]
            Ground truth list
        pred_array: List[float]
            Predictions from the model.
        """

        if not self.metrics:
            return

        assert len(y_array) == len(pred_array), 'Ground truth and predictions are not the same length!'

        names = self.metrics_names if self.metrics_names else self.metrics

        for logger in self.loggers:
            for metric, metric_name in zip(self.metrics, names):
                logger.log_scalar(self._pre(metric_name), metric(y_array, pred_array), self.epoch)


def _forward(network: nn.Module, data: DataLoader, metric: callable) -> torch.Tensor:
    device = next(network.parameters()).device

    for x, y in data:
        x, y = x.to(device), y.to(device)
        out = network(x)
        res = metric(out, y)
        yield x, y, out, res


@torch.no_grad()
def evaluate(network, data, loss_f, tracker: Tracker):
    network.eval()

    y_array = []
    preds_array = []

    for _, y, logits, loss in _forward(network, data, loss_f):
        tracker.track_loss(loss.item())

        y_array.extend(y.flatten().cpu().detach().numpy())
        # From logits to output
        preds = torch.nn.Sigmoid()(logits)
        preds_array.extend(preds.flatten().cpu().detach().numpy())

    tracker.compute_metrics(y_array, preds_array)
    return tracker.summarise(), preds_array


@torch.enable_grad()
def update(network, data, loss_f, opt, tracker: Tracker):
    network.train()
    opt.zero_grad()

    for _, _, _, loss in _forward(network, data, loss_f):
        loss.backward()
        opt.step()
        tracker.track_loss(loss.item())

        opt.zero_grad()

    return tracker.summarise()
