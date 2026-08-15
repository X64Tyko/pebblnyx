"""Keeps the server process alive only while a UI is actually attached (`Liveness`)."""

import threading
import time


class Liveness:
    """Keeps the server alive only while a UI is actually attached to it.

    The native-window build has a real signal: `webview.start()` returns when the window
    closes, and the server stops. The BROWSER build has none -- Linux ships no webview, so
    the editor opens a tab and waits on an Event that nothing ever sets. Closing the tab
    therefore did not close the editor: the process stayed, holding port 8765, and the
    next launch found something already listening and refused to start. "Closed" and
    "still running" were the same state.

    So the page says it is there, every few seconds, and stops saying it when it is gone.

    On close the page also sends an explicit goodbye, which only SHORTENS the window
    rather than exiting outright -- two tabs on one editor is a normal thing to do, and
    the second one's next heartbeat cancels the exit.
    """

    # `grace` MUST exceed the page's heartbeat interval (5s). It is the window a goodbye
    # leaves open for a surviving tab to object -- shorter than one heartbeat and closing
    # the second of two tabs would shut the editor down under the first one.
    def __init__(self, timeout=25.0, grace=8.0):
        self.timeout = timeout
        self.grace = grace
        self.last = time.time()
        self.done = threading.Event()
        self.armed = False

    def touch(self):
        self.last = time.time()

    def goodbye(self):
        self.last = min(self.last, time.time() - (self.timeout - self.grace))

    def watch(self, srv):
        """Runs on a thread; sets `done` once nothing has spoken to us in a while."""
        while not self.done.wait(2.0):
            if time.time() - self.last > self.timeout:
                print("the editor was closed -- shutting down")
                # shutdown() returns once serve_forever has stopped, so by the time the
                # main thread closes the server the listening socket is genuinely free.
                threading.Thread(target=srv.shutdown, daemon=True).start()
                self.done.set()
                return


LIVE = Liveness()


