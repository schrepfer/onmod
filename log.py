"""Common logging formatting."""

import datetime
import logging


class Formatter(logging.Formatter):
  """A logging.Formatter that supports logging microseconds (%f)."""

  def __init__(self, *args, **kwargs):
    self.last = 0
    super(Formatter, self).__init__(*args, **kwargs)

  def delta(self, created):
    x, self.last = self.last, created
    return '+%0.6f' % ((created - x) if x else 0)

  def formatTime(self, record, datefmt=None):
    if datefmt:
      return datetime.datetime.fromtimestamp(
          record.created).strftime(datefmt) + ' ' + self.delta(record.created)
    return super(Formatter, self).formatTime(record, datefmt=datefmt)


def basicConfig(level=logging.INFO,
                fmt='[%(asctime)s] %(threadName)s: %(levelname)s: %(message)s',
                datefmt='%Y/%m/%d %H:%M:%S.%f'):
  handler = logging.StreamHandler()
  handler.setFormatter(Formatter(fmt=fmt, datefmt=datefmt))
  logger = logging.getLogger()
  logger.addHandler(handler)
  logger.setLevel(level)
