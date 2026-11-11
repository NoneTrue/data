"""Cooperative report cancellation; no thread termination or GUI waits."""
from contextlib import contextmanager
from functools import wraps
import threading
import os

class ReportCancelled(Exception):
    pass

class CancellationToken:
    def __init__(self):
        self.event=threading.Event();self.lock=threading.Lock();self.state='active';self.stage='starting'
    def cancel(self):
        # The UI never waits for a filesystem commit or worker cleanup.
        if not self.lock.acquire(blocking=False):return False
        try:
            if self.state not in ['active','cancel_requested']:return False
            self.state='cancel_requested';self.event.set();return True
        finally:self.lock.release()
    def check(self,stage=None):
        if stage:self.stage=stage
        if self.event.is_set() and self.state!='committed':raise ReportCancelled('ألغيت العملية')
    def finish(self):
        with self.lock:
            self.check()
            if self.state!='committed':self.state='completed'
    def commit(self,action):
        with self.lock:
            self.check('commit');self.state='committing'
            action();self.state='committed'

_local=threading.local()
@contextmanager
def cancellation_scope(token):
    previous=getattr(_local,'token',None);_local.token=token
    try:yield
    finally:_local.token=previous

def checkpoint(stage=None):
    token=getattr(_local,'token',None)
    if token is not None:token.check(stage)

def commit_file(source,destination):
    token=getattr(_local,'token',None)
    if token is None:os.replace(source,destination)
    else:token.commit(lambda:os.replace(source,destination))

def cancellable(fn):
    """Optional cancel=token for direct calls; inherit Task scope otherwise."""
    @wraps(fn)
    def wrapped(*args,**kwargs):
        token=kwargs.pop('cancel',None) or getattr(_local,'token',None)
        with cancellation_scope(token):
            checkpoint();result=fn(*args,**kwargs);checkpoint();return result
    return wrapped

class CheckedWriter:
    """Observe cancellation during python-docx's non-context-managed ZIP save.

    Let that writer close its ZIP normally; the caller raises immediately after
    save returns, before validation/commit. Raising from write itself can leave
    python-docx's ZipFile destructor holding a closed external file.
    """
    def __init__(self,stream):self.stream=stream;self.cancelled=False
    def __getattr__(self,name):return getattr(self.stream,name)
    def write(self,data):
        try:checkpoint('archive_write')
        except ReportCancelled:self.cancelled=True
        return self.stream.write(data)
