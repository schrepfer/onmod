#!/usr/bin/env python3
# vim: ft=python

"""Execute command on modification of watched files."""

import argparse
import collections
import datetime
import io
import logging
import os
import pipes
import pprint
import psutil
import select
import signal
import subprocess
import sys
import time
import threading

import cmd
import log

# TODO: Add looping so that it reruns possibly?
#       Add ability to kill existing run if new change detected.

def DefineFlags():
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v', '--verbosity',
      action='store',
      default=20,
      type=int,
      help='the logging verbosity',
      metavar='LEVEL')
  parser.add_argument(
      '-V', '--version',
      action='version',
      version='%(prog)s version 0.1')
  parser.add_argument(
      '-t', '--sleep',
      help='time to sleep between checks',
      metavar='SECONDS',
      default=5,
      type=int,
  )
# parser.add_argument(
#     '--timeout',
#     help='time in seconds that missing files timeout',
#     metavar='SECONDS',
#     default=600,
#     type=int,
# )
  parser.add_argument(
      '-f', '--files',
      help='files to watch for mtime changes',
      metavar='FILES',
      nargs='*',
      type=str,
  )
  parser.add_argument(
      '-k', '--kill',
      help='kill process (and children) if watched files change',
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-l', '--loop',
      help=(
          'run process continually after completion, requires that you set '
          '--kill'),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-r', '--retry_on_error',
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
      '-w', '--wait',
      help=(
          'wait additional sleep cycles after files changed, till there are '
          'no more changes before executing the CMD'),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-m',
      '--wait_for_mod',
      help=(
          'wait for files to change once before executing the CMD the first '
          'time'),
      default=False,
      action='store_true',
  )
  parser.add_argument(
      '-s',
      '--sub',
      help=(
          'substitute the character specified for the files that changed in '
          'the CMD'),
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
  CheckFlags(parser, args)
  return args


def CheckFlags(parser, args):
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not args.cmd or not args.cmd[0]:
    parser.error('CMD must be set')
  if not args.files:
    parser.error('--files must be set')
  if args.loop and not args.kill:
    parser.error('do not set --loop without --kill')
  if args.max_retries < 1:
    parser.error('--max_retries must be >= 1')


def LogTime(t0, t1, exit_status=None):
  buf = io.StringIO()
  buf.write('\n')
  buf.write('----------------------------------------\n')
  if exit_status:
    buf.write('        FAILED! FAILED! FAILED!\n')
    buf.write('----------------------------------------\n')
  buf.write(' End Time: %s\n' % time.strftime(
      '%Y/%m/%d %H:%M:%S', time.localtime(t1)))
  buf.write(' Elapsed Time: %s\n' % datetime.timedelta(microseconds=(t1-t0)*1e6))
  buf.write('----------------------------------------')
  logging.info(buf.getvalue())


class Runner(threading.Thread):

  # TODO: subprocess.Popen to pass preexec_fn=os.setsid
  #       os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)

  def __init__(self, *cmds, max_retries=0, loop=False, callback=None, cwd='.'):
    super().__init__()
    self.setDaemon(True)
    self.lock = threading.Lock()
    self.cwd = cwd
    self.cmds = cmds
    self.max_retries = max_retries
    self.loop = loop
    self.callback = callback
    self.proc = None
    self.start_time = None
    self.end_time = None
    self.name = None

  def run(self):
    for c in self.cmds:
      if not c:
        continue
      attempt = 0
      while attempt < self.max_retries:
        attempt += 1
        cmd.Print(c)
        with self.lock:
          self.start_time = time.time()
          self.name = c[0]
          try:
            os.chdir(self.cwd)
            self.proc = subprocess.Popen(c)
          except FileNotFoundError as e:
            logging.error('%s', e)
            return
          except OSError as e:
            logging.error('%s', e)
            continue
        ret = self.proc.wait()
        self.end_time = time.time()
        if ret is not None:
          LogTime(self.start_time, self.end_time, exit_status=ret)
        with self.lock:
          if not self.proc:
            # self.proc cleared by the kill() method.
            return
          self.proc = None
        if ret == 0:
          if not self.loop:
            break
          # Reset successful attempts
          attempt = 0

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


def main(args):
  logging.info('Args:\n%s', pprint.pformat(dict(args.__dict__.items()), indent=1))

  if args.wait_for_mod:
    mtimes = {f: os.stat(f).st_mtime for f in args.files}
    diff_detected = False
  else:
    mtimes = {f: 0 for f in args.files}
    diff_detected = True
  failed = collections.defaultdict(int)
  diff_files = set()
  first = True
  runner = None
  force = False
  removed = set()
  cwd = os.getcwd()
  disp_msg = False
  try:
    while True:
      try:
        sf = {f: os.stat(f).st_mtime for f in mtimes}
        diff = set(mtimes.items()).symmetric_difference(sf.items())
        if diff:
          if not first:
            logging.info('File mtime change detected:\n\t%s', '\n\t'.join(
                sorted(set(x[0] for x in diff))))
          first = False
          mtimes = sf
          diff_detected = True
          diff_files.update(x[0] for x in diff)
          if args.wait:
            logging.info('Waiting to see if there are more changes...')
        # Move this to a separate iteration to allow multiple changes.
        if force or (diff_detected and (not diff or not args.wait)):
          if args.wait:
            logging.info('Continuing...')
          # This duplicates some of what's already done with xargs. Consider
          # piping to that instead of re-building the logic.
          cc = []
          cmds = []
          for c in args.cmd:
            if c == '&&':
              cmds.append(cc)
              cc = []
            # Consider substr here. Only currently works if {} is by itself.
            elif c == args.sub:
              cc.extend(pipes.quote(f) for f in sorted(diff_files))
            else:
              cc.append(c)
          cmds.append(cc)

          if runner:
            if args.kill:
              runner.kill()
            runner.join()

          runner = Runner(*cmds, max_retries=args.max_retries, loop=args.loop, cwd=cwd)
          runner.start()

          diff_files = set()
          diff_detected = False
        failed = collections.defaultdict(int)
      except OSError as e:
        if e.filename:
          c = failed[e.filename] = failed[e.filename] + 1
          logging.warning('%s (%d)', e, c)
#         if mtimes[e.filename] and c >= 10 and (
#             time.time() - mtimes[e.filename]) > args.timeout:
          if c >= 10:
            logging.warning('Removing file from watch list: %s', e.filename)
            del mtimes[e.filename]
            removed.add(e.filename)
      force = False
      if not mtimes and not disp_msg:
        logging.info('No more files being watched.')
        print('\n[Press `Enter` to re-add removed files]')
        disp_msg = True
      if sys.stdin in select.select([sys.stdin], [], [], args.sleep)[0]:
        line = sys.stdin.readline().strip()

        if not mtimes and removed:
          logging.info('Adding back removed files to watch list:\n\t%s', '\n\t'.join(sorted(removed)))
          #t = time.time()
          for f in removed:
            mtimes[f] = 0
          removed.clear()

        v = {
            'mtimes': {k: (t, str(datetime.datetime.fromtimestamp(t))) for k, t in mtimes.items()},
            'removed': removed,
        }
        logging.info('%s', int(os.environ.get('COLUMNS', 80)))
        logging.info('Vars:\n%s', pprint.pformat(dict(v.items()), indent=1, width=int(os.environ.get('COLUMNS', 80))))
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
  a = DefineFlags()
  log.BasicConfig(level=a.verbosity)
  sys.exit(main(a))
