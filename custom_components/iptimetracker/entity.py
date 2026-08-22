from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

from .const import AVAILABILITY_GRACE


class GracefulAvailabilityMixin:
    """Tolerate a brief unhealthy read instead of flapping unavailable.

    CoordinatorEntity's default `available` (and any check layered on top of
    it) flips the instant the underlying condition is False, so a single
    transient hiccup - a failed poll, an ancillary sub-request like the WAN
    link check coming back empty - makes the entity unavailable immediately.
    Mix this in before CoordinatorEntity (so its `available` wins over
    CoordinatorEntity's) and override `_is_healthy` with whatever "no news
    is good news" signal applies; unavailable is only reported once that
    has been False continuously for more than AVAILABILITY_GRACE.
    """

    _unavailable_since: datetime | None = None

    @property
    def _is_healthy(self) -> bool:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        if self._is_healthy:
            self._unavailable_since = None
            return True
        now = dt_util.utcnow()
        if self._unavailable_since is None:
            self._unavailable_since = now
        return (now - self._unavailable_since).total_seconds() < AVAILABILITY_GRACE
