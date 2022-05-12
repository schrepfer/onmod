#!/usr/bin/env python3

"""Common cmd libraries."""

import os
import pipes
import sys


def Print(argv):
  try:
    sys.stderr.write('< %s\n' % os.getcwd())
  except OSError:
    pass
  sys.stderr.write('>')
  length = 0
  first = True
  for q in argv:
    out = pipes.quote(q)
    if not first and length + len(out) > 100:
      sys.stderr.write(' \\\n ')
      length = 0
    sys.stderr.write(' %s' % out)
    length += 1 + len(out)
    first = False
  sys.stderr.write('\n\n')
