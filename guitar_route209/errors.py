"""Error types carrying precise rejection locations."""


class MidiRejection(Exception):
    """Raised when uploaded material is invalid.

    Carries enough context to locate the offending material:
    which file ("reference" / "performance"), which track (0-based,
    None when not track specific) and which event (0-based index inside
    the track, None when not event specific).
    """

    def __init__(self, message, file=None, track=None, event=None,
                 action=None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.track = track
        self.event = event
        self.action = action

    def to_detail(self):
        return {
            "message": self.message,
            "file": self.file,
            "track": self.track,
            "event": self.event,
            "action": self.action,
        }
