#!/usr/bin/env python3
# vim: ft=python

"""Execute command on modification of watched files."""

import argparse
import cmd
import collections
import datetime
import enum
import io
import logging
import os
import pprint
import select
import shlex
import subprocess
import sys
import threading
import time
from typing import Callable, Optional
import log
import psutil

# TODO: Add looping so that it reruns possibly?
#       Add ability to kill existing run if new change detected.


def define_flags() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v',
      '--verbosity',
      action='store',
      default=20,
      type=int,
      help='the logging verbosity',
      metavar='LEVEL',
  )
  parser.add_argument(
      '-V', '--version', action='version', version='%(prog)s version 0.1'
  )
  parser.add_argument(
      '-t',
      '--sleep',
      help='time to sleep between checks',
      metavar='SECONDS',
      default=5,
      type=int,
  )
  parser.add_argument(
      '-f',
      '--files',
      help='files to watch for mtime changes',
      metavar='FILES',
      nargs='*',
      type=str,
  )
  parser.add_argument(
      '-k',
      '--kill',
      help='kill process (and children) if watched files change',
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-l',
      '--loop',
      help=(
          'run process continually after completion, requires that you set '
          '--kill'
      ),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-r',
      '--retry_on_error',
      help='retry the previous CMD after failures',
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '--max_retries',
      help='maximum number of times to attempt each CMD',
      metavar='COUNT',
      default=1,
      type=int,
  )
  parser.add_argument(
      '-w',
      '--wait',
      help=(
          'wait additional sleep cycles after files changed, till there are '
          'no more changes before executing the CMD'
      ),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-m',
      '--wait_for_mod',
      help=(
          'wait for files to change once before executing the CMD the first '
          'time'
      ),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-s',
      '--sub',
      help=(
          'substitute the character specified for the files that changed in '
          'the CMD'
      ),
      metavar='STRING',
      action='store',
      default='{}',
      type=str,
  )
  parser.add_argument(
      'cmd',
      help='cmd to execute',
      metavar='CMD',
      nargs='*',
      type=str,
  )

  args = parser.parse_args()
  check_flags(parser, args)
  return args


def check_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not args.cmd or not args.cmd[0]:
    parser.error('CMD must be set')
  if not args.files:
    parser.error('--files must be set')
  if args.loop and not args.kill:
    parser.error('do not set --loop without --kill')
  if args.max_retries < 1:
    parser.error('--max_retries must be >= 1')


def logTime(t0, t1, exit_status=None, required=False):
  buf = io.StringIO()
  buf.write('\n')
  buf.write('------------------------------------------\n')
  if exit_status:
    buf.write('         FAILED! FAILED! FAILED!\n')
    buf.write('------------------------------------------\n')
  buf.write(
      ' End Time: %s\n' % time.strftime('%Y/%m/%d %H:%M:%S', time.localtime(t1))
  )
  buf.write(
      ' Elapsed Time: %s\n' % datetime.timedelta(microseconds=(t1 - t0) * 1e6)
  )
  if exit_status and required:
    buf.write('------------------------------------------\n')
    buf.write('       CLEAN EXIT STATUS REQUIRED\n')
    buf.write('     NEXT COMMAND WILL NOT EXECUTE!\n')
  buf.write('------------------------------------------')
  logging.info(buf.getvalue())


class Command(object):
  required: bool
  args: list[str]

  def __init__(self, args: list[str], required: bool = False):
    self.args = args
    self.required = required

  def name(self) -> str:
    return self.args[0]

  def print(self) -> None:
    cmd.Print(self.args)


class Runner(threading.Thread):

  daemon: bool
  lock: threading.Lock
  cwd: str
  cmds: tuple[Command, ...]
  max_retries: int
  loop: bool
  callback: Optional[Callable[[], None]]
  proc: Optional[subprocess.Popen]
  start_time: Optional[float]
  end_time: Optional[float]
  name: str

  # TODO: subprocess.Popen to pass preexec_fn=os.setsid
  #       os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)

  def __init__(
      self, *cmds: Command, max_retries: int = 0, loop: bool = False, callback: Optional[Callable[[], None]] = None, cwd='.'
  ):
    super().__init__()
    self.daemon = True
    self.lock = threading.Lock()
    self.cwd = cwd
    self.cmds = cmds
    self.max_retries = max_retries
    self.loop = loop
    self.callback = callback
    self.proc = None
    self.start_time = None
    self.end_time = None
    self.name = ''

  def run_once(self) -> Optional[bool]:
    success = True
    for cmd in self.cmds:
      if not cmd:
        continue
      cmd.print()
      result = self.execute_command(cmd)
      success = success and result
    return success

  def execute_command(self, cmd) -> bool:
    with self.lock:
      self.start_time = time.time()
      self.name = cmd.name
      try:
        os.chdir(self.cwd)
        self.proc = subprocess.Popen(cmd.args)
      except FileNotFoundError as e:
        logging.error('%s', e)
        return False
      except OSError as e:
        if cmd.required:
          logging.error('%s', e)
          return False
        logging.warning('%s', e)
        return True  # Continue processing other commands
    ret = self.proc.wait()
    self.end_time = time.time()
    logTime(
        self.start_time, self.end_time, exit_status=ret, required=cmd.required
    )
    with self.lock:
      if not self.proc:
        # self.proc cleared by the kill() method.
        return False
      self.proc = None
    return ret == 0

  def run(self):
    attempt = 0
    while attempt < self.max_retries:
      attempt += 1
      if attempt > 1:
        logging.warning('[%d] Running all commands again', attempt)
      success = self.run_once()
      if success:
        if not self.loop:
          break
        attempts = 0
      elif success is None:
        break

    if self.callback:
      self.callback()

  def kill(self):
    with self.lock:
      if not self.proc:
        return
      try:
        if self.proc.poll() is None:
          logging.info('Killing %r..', self.name)
          for child in psutil.Process(self.proc.pid).children(recursive=True):
            try:
              child.terminate()
              child.wait(timeout=10)
            except psutil.NoSuchProcess as e:
              logging.error('%s', e)
            except psutil.TimeoutExpired as e:
              logging.error('%s', e)
          self.proc.terminate()  # or kill()
          self.proc.wait(timeout=10)
      except psutil.NoSuchProcess:
        pass
      finally:
        self.proc = None


def update_mtimes(files, wait_for_mod) -> dict[str, float]:
  return (
      {f: os.stat(f).st_mtime for f in files}
      if wait_for_mod
      else {f: 0 for f in files}
  )


def handle_diff(mtimes, sf) -> tuple[bool, set[str]]:
  diff = set(mtimes.items()).symmetric_difference(sf.items())
  return bool(diff), {x[0] for x in diff}


def process_commands(args, diff_files) -> list[Command]:
  cmds = []
  cc: list[str] = []
  for c in args.cmd:
    if c in {'&&', '||', ';'}:
      cmds.append(Command(cc, required=(c == '&&')))
      cc = []
    elif c == args.sub:
      cc.extend(shlex.quote(f) for f in sorted(diff_files))
    else:
      cc.append(c)
  if cc:
    cmds.append(Command(cc, required=False))
  return cmds


def handle_removed_files(mtimes, removed) -> None:
  for f in removed:
    mtimes[f] = 0
  removed.clear()


def log_vars(mtimes, removed) -> None:
  v = {
      'mtimes': {
          k: (t, str(datetime.datetime.fromtimestamp(t)))
          for k, t in mtimes.items()
      },
      'removed': removed,
  }
  logging.info('%s', int(os.environ.get('COLUMNS', 80)))
  logging.info(
      'Vars:\n%s',
      pprint.pformat(v, indent=1, width=os.get_terminal_size().columns),
  )


def main(args: argparse.Namespace) -> int:
  logging.info('Args:\n%s', pprint.pformat(vars(args), indent=1))

  if args.wait_for_mod:
    mtimes = {f: os.stat(f).st_mtime for f in args.files}
    diff_detected = False
  else:
    mtimes = {f: 0 for f in args.files}
    diff_detected = True

  failed: dict[str, int] = collections.defaultdict(int)
  diff_files = set()
  previous_diff_files: set[str] = set()
  force = False
  removed = set()
  cwd = os.getcwd()
  disp_msg = False
  runner = None

  try:
    first = True
    while True:
      try:
        new_mtimes = {f: os.stat(f).st_mtime for f in mtimes}
        new_diff, new_diff_files = handle_diff(mtimes, new_mtimes)

        # If it happens that we don't have any diffs, and we forced, then we'll
        # just copy the diffs from the previous output.
        if force and not new_diff:
          logging.info('No new differences found, forcing previous diff.')
          new_diff, new_diff_files = True, previous_diff_files.copy()

        if new_diff:
          if not first:
            logging.info(
                'File mtime change detected:\n\t%s',
                '\n\t'.join(sorted(new_diff_files)),
            )
          first = False
          mtimes = new_mtimes
          diff_detected = True
          diff_files.update(new_diff_files)

          if args.wait:
            logging.info('Waiting to see if there are more changes...')

        # If we have force enabled (via pressing enter) or we have a diff and
        # wait is enabled, that means we wait another loop cycle and check to
        # make sure no extra diffs were detected.
        if diff_detected and not (new_diff and args.wait):
          if args.wait:
            logging.info('Continuing...')

          cmds = process_commands(args, diff_files)

          if runner:
            if args.kill:
              runner.kill()
            runner.join()

          runner = Runner(
              *cmds, max_retries=args.max_retries, loop=args.loop, cwd=cwd
          )
          runner.start()

          previous_diff_files = diff_files.copy()
          diff_files.clear()
          diff_detected = False

        failed = collections.defaultdict(int)
      except OSError as e:
        if e.filename:
          failed[e.filename] += 1
          logging.warning('%s (%d)', e, failed[e.filename])
          if failed[e.filename] >= 10:
            logging.warning('Removing file from watch list: %s', e.filename)
            del mtimes[e.filename]
            removed.add(e.filename)
      force = False
      if not mtimes and not disp_msg:
        logging.info('No more files being watched.')
        print('\n[Press `Enter` to re-add removed files]')
        disp_msg = True
      if sys.stdin in select.select([sys.stdin], [], [], args.sleep)[0]:
        sys.stdin.readline()
        if not mtimes and removed:
          logging.info(
              'Adding back removed files to watch list:\n\t%s',
              '\n\t'.join(sorted(removed)),
          )
          handle_removed_files(mtimes, removed)
        log_vars(mtimes, removed)
        force = True
        disp_msg = False
  except KeyboardInterrupt:
    print()
  finally:
    if runner:
      runner.kill()
      runner.join()
  return 1


if __name__ == '__main__':
  a = define_flags()
  log.basicConfig(level=a.verbosity)
  sys.exit(main(a))
