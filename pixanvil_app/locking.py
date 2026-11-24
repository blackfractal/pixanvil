"""One job coordinator per app-state directory, including across CLI launches."""
import os


class InstanceLock:
    def __init__(self,path):
        self.file=None
        stream=open(path,'a+b')
        if stream.tell()==0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError('PixAnvil is already running for this state directory. Open the existing gallery.')
        self.file=stream

    def close(self):
        if self.file:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close(); self.file=None
