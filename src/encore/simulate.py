"""Replay recorded history through a proposed watch policy (issue #58).

F10's defaults are quiet by design and opting into EPs or singles is one
command — but the cost of that command is invisible until a week of alerts has
landed. This replays what encore already recorded and says what a proposed
policy *would* have delivered, per channel, before anything changes.

## Why this reads release-groups and not the event log

The obvious implementation replays `events`. It would be wrong, and wrong in
the direction that matters.

**Type filters gate event creation, not delivery** (`encore.watch.engine`): a
release-group whose type tags are not opted in is *recorded* and raises no
event at all. So under an albums-only policy no single ever became an event,
and a replay of the event log would report "widening to singles: +0" — an
absence (no events, because they were filtered before they existed) rendered
as a measurement (no additional noise). That is the exact question the command
exists to answer, so getting it wrong here would be worse than not having it.

The corpus is therefore `release_groups`, which encore records exactly and on
purpose — the diff must stay exact "so a later opt-in starts from truth". The
event log is still read, as ground truth for what actually happened; the
reconstruction only fills in what did not.

## The blind spot, named rather than papered over

An artist's **first** poll is silent by design (`docs/adr/0011`): the whole
back catalogue is inventoried and raises no ``new`` events whatever the policy
says. A group first seen during that poll cannot be replayed under any policy.

Rather than guess at a tolerance for "which rows belong to the baseline poll",
this module uses an exact test: an artist whose earliest recorded group falls
inside the simulated window was baselined inside it, so its groups are
excluded and counted under `artists_baselined_in_window`. An artist baselined
before the window has every in-window group post-baseline, and those replay
exactly.

Two more gaps are counted rather than smoothed: days in the window with no
recorded observation at all (a watch run that did not happen is not a quiet
day), and groups the reconstruction cannot explain — allowed by the policy in
force, post-baseline, and yet carrying no event, which should not happen and
is reported instead of being silently dropped.

Everything here is pure and offline. No network, no clock of its own: callers
pass the window and the corpus.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from encore.artistsettings import (
    ArtistWatchSettings,
    SettingsOverride,
    stored_secondary_types,
)
from encore.channelroute import ChannelRoute, RoutableEvent, channel_accepts
from encore.models import Artist, ReleaseGroup
from encore.storage import Storage
from encore.watch.engine import parse_earliest_date

__all__ = [
    "CADENCE_DIGEST",
    "CADENCE_INSTANT",
    "OUTCOME_BASELINE",
    "OUTCOME_DELIVERED",
    "OUTCOME_FILTERED",
    "OUTCOME_MUTED",
    "OUTCOME_ROUTED_AWAY",
    "OUTCOME_UNEXPLAINED",
    "Delivered",
    "Observation",
    "Outcome",
    "OutcomeChange",
    "SimulationChannel",
    "SimulationReport",
    "build_corpus",
    "diff_reports",
    "format_report",
    "report_payload",
    "run_simulation",
    "simulate",
]

# A group whose type tags the policy does not allow: recorded, never announced.
OUTCOME_FILTERED = "filtered"
# The policy allows it, but the artist is muted on the day it was observed.
OUTCOME_MUTED = "muted"
# Allowed and unmuted, but no channel's route accepted it (or no channel
# existed yet). An event exists; nothing was sent.
OUTCOME_ROUTED_AWAY = "routed_away"
# Allowed, unmuted, and at least one channel would have received it.
OUTCOME_DELIVERED = "delivered"
# The artist's first poll fell inside the window: its back catalogue is silent
# under every policy, so it cannot be replayed under any of them.
OUTCOME_BASELINE = "baseline"
# Allowed by the policy in force, post-baseline, and yet no event was recorded.
# Should not happen. Reported rather than dropped.
OUTCOME_UNEXPLAINED = "unexplained"

CADENCE_INSTANT = "instant"
CADENCE_DIGEST = "digest"

_UNRECONSTRUCTIBLE = frozenset({OUTCOME_BASELINE, OUTCOME_UNEXPLAINED})


@dataclass(frozen=True)
class Observation:
    """One recorded thing in the window that a policy has an opinion about.

    Either a release-group first seen in the window (a potential ``new`` or
    ``upcoming``) or a recorded ``date_changed``. ``recorded_kind`` is the
    event actually written at the time, or ``None`` when the policy in force
    filtered it — which is precisely the case the reconstruction exists for.
    """

    artist_mbid: str
    artist_name: str
    release_group_mbid: str
    title: str
    primary_type: str | None
    secondary_types: tuple[str, ...]
    observed_at: dt.datetime
    counterfactual_kind: str
    recorded_kind: str | None
    artist_baselined_in_window: bool
    library_keys: frozenset[str]
    artist_keys: frozenset[str]
    source: str

    @property
    def observed_on(self) -> dt.date:
        """The day this was observed, in the window's own timezone (UTC)."""
        return self.observed_at.date()


@dataclass(frozen=True)
class SimulationChannel:
    """One channel as the replay needs it — no secrets, no delivery state."""

    name: str
    mode: str
    created_at: dt.datetime
    route: ChannelRoute = field(default_factory=ChannelRoute)


@dataclass(frozen=True)
class Delivered:
    """One (observation, channel) obligation the replay says would exist."""

    channel: str
    cadence: str


@dataclass(frozen=True)
class Outcome:
    """What one observation would have done under the policy being simulated."""

    observation: Observation
    outcome: str
    deliveries: tuple[Delivered, ...] = ()

    @property
    def delivered(self) -> bool:
        """Whether any channel would have received it."""
        return self.outcome == OUTCOME_DELIVERED

    @property
    def channels(self) -> tuple[str, ...]:
        """The names of the channels that would have received it."""
        return tuple(delivery.channel for delivery in self.deliveries)


def _routable(observation: Observation, policy: ArtistWatchSettings) -> RoutableEvent:
    """Describe one observation in the terms a channel route may ask about."""
    return RoutableEvent(
        priority=policy.priority,
        primary_type=observation.primary_type,
        secondary_types=observation.secondary_types,
        library_keys=observation.library_keys,
        artist_keys=observation.artist_keys,
        source=observation.source,
    )


def _cadence(channel_mode: str, priority: str) -> str:
    """Which stream one delivery would ride, mirroring `notify.engine`.

    An ``instant`` artist breaks through digest windows on every channel; a
    ``digest`` artist waits for the window even on an instant channel;
    ``normal`` follows the channel's own mode.
    """
    if priority == CADENCE_INSTANT:
        return CADENCE_INSTANT
    if priority == CADENCE_DIGEST:
        return CADENCE_DIGEST
    return CADENCE_INSTANT if channel_mode == CADENCE_INSTANT else CADENCE_DIGEST


def _resolve_one(
    observation: Observation,
    policy: ArtistWatchSettings,
    channels: tuple[SimulationChannel, ...],
    *,
    policy_in_force: ArtistWatchSettings,
) -> Outcome:
    """Decide one observation's fate, in the real delivery path's own order."""
    if observation.artist_baselined_in_window:
        # Silent under every policy: nothing to compare.
        return Outcome(observation, OUTCOME_BASELINE)
    allowed = policy.passes(observation.primary_type, observation.secondary_types)
    if not allowed:
        return Outcome(observation, OUTCOME_FILTERED)
    if observation.recorded_kind is None and policy_in_force.passes(
        observation.primary_type, observation.secondary_types
    ):
        # The policy in force allowed it and it is post-baseline, so an event
        # should exist. It does not. Say so rather than counting it either way.
        return Outcome(observation, OUTCOME_UNEXPLAINED)
    if policy.is_muted_on(observation.observed_on):
        return Outcome(observation, OUTCOME_MUTED)
    routable = _routable(observation, policy)
    reached = tuple(
        Delivered(channel.name, _cadence(channel.mode, policy.priority))
        for channel in channels
        # A channel never replays history: an event older than the channel
        # fans out to nothing (`Storage.ensure_deliveries`).
        if observation.observed_at >= channel.created_at
        and channel_accepts(routable, channel.route)
    )
    if not reached:
        return Outcome(observation, OUTCOME_ROUTED_AWAY)
    return Outcome(observation, OUTCOME_DELIVERED, deliveries=reached)


@dataclass(frozen=True)
class SimulationReport:
    """One replay's outcomes, with everything it could not see counted."""

    window_start: dt.datetime
    window_end: dt.datetime
    outcomes: tuple[Outcome, ...]
    channels: tuple[SimulationChannel, ...]
    artists_baselined_in_window: tuple[str, ...]
    days_without_observations: int

    @property
    def observations(self) -> int:
        """Every recorded observation the window contains."""
        return len(self.outcomes)

    @property
    def replayable(self) -> int:
        """Observations the replay can actually reason about."""
        return sum(1 for o in self.outcomes if o.outcome not in _UNRECONSTRUCTIBLE)

    def counts_by_outcome(self) -> dict[str, int]:
        """How many observations landed in each outcome bucket."""
        return dict(Counter(o.outcome for o in self.outcomes))

    def deliveries_by_channel(self) -> dict[str, int]:
        """One count per channel — the figure the real path materialises."""
        counter: Counter[str] = Counter()
        for outcome in self.outcomes:
            for name in outcome.channels:
                counter[name] += 1
        # Total over configured channels: a channel that would receive nothing
        # must read as 0 rather than being absent from the table.
        return {channel.name: counter.get(channel.name, 0) for channel in self.channels}

    @property
    def deliveries(self) -> int:
        """Total delivery obligations across every channel."""
        return sum(self.deliveries_by_channel().values())

    def by_artist(self) -> dict[str, int]:
        """Deliveries per artist display name, delivered rows only."""
        counter: Counter[str] = Counter()
        for outcome in self.outcomes:
            if outcome.delivered:
                counter[outcome.observation.artist_name] += len(outcome.channels)
        return dict(counter)

    def by_type(self) -> dict[str, int]:
        """Deliveries per MusicBrainz primary type, delivered rows only."""
        counter: Counter[str] = Counter()
        for outcome in self.outcomes:
            if outcome.delivered:
                label = outcome.observation.primary_type or "(untyped)"
                counter[label] += len(outcome.channels)
        return dict(counter)

    def by_day(self) -> dict[dt.date, int]:
        """Deliveries per day, delivered rows only."""
        counter: Counter[dt.date] = Counter()
        for outcome in self.outcomes:
            if outcome.delivered:
                counter[outcome.observation.observed_on] += len(outcome.channels)
        return dict(counter)

    def noisiest(self, limit: int = 10) -> list[tuple[str, int]]:
        """Return the loudest artists, most first, ties broken by name."""
        return sorted(self.by_artist().items(), key=lambda item: (-item[1], item[0]))[:limit]


def simulate(
    observations: tuple[Observation, ...],
    channels: tuple[SimulationChannel, ...],
    policies: dict[str, ArtistWatchSettings],
    policies_in_force: dict[str, ArtistWatchSettings],
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    days_without_observations: int,
) -> SimulationReport:
    """Replay ``observations`` under ``policies`` — pure, offline, deterministic.

    ``policies_in_force`` is what the install is running today. It is not used
    to decide anything the simulated policy decides; it is used only to tell a
    type-filtered group (no event, correctly) apart from a group that should
    have raised one and did not (no event, unexplained). Reading those as the
    same thing would let a data problem read as a policy result.

    Raises:
        KeyError: an observation names an artist absent from ``policies``. The
            mapping must be total — an absent policy silently defaulting to
            "no restriction" is issue #33's shape, and it would make a
            simulated widening look free.
    """
    resolved = tuple(
        _resolve_one(
            observation,
            policies[observation.artist_mbid],
            channels,
            policy_in_force=policies_in_force[observation.artist_mbid],
        )
        for observation in observations
    )
    baselined = tuple(
        sorted(
            {
                observation.artist_mbid
                for observation in observations
                if observation.artist_baselined_in_window
            }
        )
    )
    return SimulationReport(
        window_start=window_start,
        window_end=window_end,
        outcomes=resolved,
        channels=channels,
        artists_baselined_in_window=baselined,
        days_without_observations=days_without_observations,
    )


@dataclass(frozen=True)
class OutcomeChange:
    """One observation whose fate differs between two policies."""

    observation: Observation
    before: str
    after: str


def diff_reports(before: SimulationReport, after: SimulationReport) -> list[OutcomeChange]:
    """Observations whose outcome changed between two replays of one window.

    Both reports must come from the same corpus; the pairing is positional,
    which is what `simulate` guarantees by preserving input order.
    """
    if before.observations != after.observations:
        raise ValueError("cannot diff replays of different corpora")
    return [
        OutcomeChange(a.observation, b.outcome, a.outcome)
        for b, a in zip(before.outcomes, after.outcomes, strict=True)
        if b.outcome != a.outcome
    ]


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Attach UTC to a naive timestamp — SQLite round-trips them without one.

    Comparing a naive stored timestamp against an aware window boundary raises
    outright, and quietly dropping such rows would shrink the corpus without
    saying so — which is the failure this whole module exists to avoid.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _counterfactual_kind(group: ReleaseGroup) -> str:
    """Return the kind a group would raise if the policy admitted it.

    Mirrors `encore.watch.engine._kind_for_unseen`: a strictly future-dated
    group is an announcement, anything else is news. The comparison is against
    the day the group was *first seen*, not today, so a release that has since
    come out still replays as the ``upcoming`` it was at the time.
    """
    earliest = parse_earliest_date(group.first_release_date)
    return (
        "upcoming"
        if earliest is not None and earliest > _as_utc(group.first_seen_at).date()
        else "new"
    )


def _secondary_types(group: ReleaseGroup) -> tuple[str, ...]:
    """Read a group's stored secondary types through the one shared reader.

    This used to tolerate an unreadable blob by answering `()`, which made the
    replay disagree with delivery in the one direction a simulation must not:
    it showed a suppressed release passing. `stored_secondary_types` answers
    with a slug no allowlist can contain instead.
    """
    return stored_secondary_types(group.secondary_types_json)


def _observation(
    group: ReleaseGroup,
    names: dict[str, str],
    owners: dict[str, list[Artist]],
    *,
    observed_at: dt.datetime,
    counterfactual_kind: str,
    recorded_kind: str | None,
    baselined_in_window: bool,
) -> Observation:
    """Build one `Observation` from a stored group and its live Plex owners."""
    rows = owners.get(group.artist_mbid, [])
    return Observation(
        artist_mbid=group.artist_mbid,
        # An identity with no ArtistMatch name is reported as unnamed rather
        # than as its MBID: the report groups by display name, and a bare MBID
        # in that column reads as an artist actually called that.
        artist_name=names.get(group.artist_mbid) or "(unnamed identity)",
        release_group_mbid=group.mbid,
        title=group.title,
        primary_type=group.primary_type,
        secondary_types=_secondary_types(group),
        observed_at=observed_at,
        counterfactual_kind=counterfactual_kind,
        recorded_kind=recorded_kind,
        artist_baselined_in_window=baselined_in_window,
        library_keys=frozenset(row.library_key for row in rows),
        artist_keys=frozenset(row.plex_rating_key for row in rows),
        source="plex" if rows else "promoted",
    )


def _date_change_observations(
    groups: Sequence[ReleaseGroup],
    kinds_by_group: dict[int, list[tuple[str, dt.datetime]]],
    names: dict[str, str],
    owners: dict[str, list[Artist]],
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    baselined: Callable[[str], bool],
) -> list[Observation]:
    """Replay the one event kind a group row cannot reconstruct.

    A ``date_changed`` is a revision to a date the row now holds outright, so
    the row remembers the destination and not the journey. These come from the
    event log or not at all.
    """
    by_id = {group.id: group for group in groups if group.id is not None}
    found: list[Observation] = []
    for group_id, entries in sorted(kinds_by_group.items()):
        group = by_id.get(group_id)
        if group is None:  # pragma: no cover - a foreign key guarantees one
            continue
        for kind, created in entries:
            if kind != "date_changed" or not (window_start <= created <= window_end):
                continue
            found.append(
                _observation(
                    group,
                    names,
                    owners,
                    observed_at=created,
                    counterfactual_kind="date_changed",
                    recorded_kind="date_changed",
                    baselined_in_window=baselined(group.artist_mbid),
                )
            )
    return found


def build_corpus(
    storage: Storage, window_start: dt.datetime, window_end: dt.datetime
) -> tuple[tuple[Observation, ...], tuple[SimulationChannel, ...], int]:
    """Assemble what `simulate` replays over one window.

    Returns ``(observations, channels, days_without_observations)``.

    Two kinds of observation:

    * a release-group whose ``first_seen_at`` falls in the window — a potential
      ``new`` or ``upcoming``, reconstructible whether or not it raised an event;
    * a recorded ``date_changed`` event in the window, which is the one kind a
      group row cannot reconstruct, because the row holds the current date and
      not the history of revisions.

    An artist whose *earliest* recorded group falls inside the window was
    baselined inside it, so its back catalogue is silent under every policy
    (`docs/adr/0011`) and its observations are flagged rather than replayed.
    """
    groups = storage.all_release_groups()
    baseline_at: dict[str, dt.datetime] = {}
    for group in groups:
        seen = _as_utc(group.first_seen_at)
        if group.artist_mbid not in baseline_at or seen < baseline_at[group.artist_mbid]:
            baseline_at[group.artist_mbid] = seen
    events = storage.list_events()
    kinds_by_group: dict[int, list[tuple[str, dt.datetime]]] = {}
    for event in events:
        kinds_by_group.setdefault(event.release_group_id, []).append(
            (event.kind, _as_utc(event.created_at))
        )
    mbids = sorted({group.artist_mbid for group in groups})
    names = storage.match_names_by_mbids(mbids)
    owners = storage.owners_by_mbid(mbids)
    channels = tuple(
        SimulationChannel(
            name=row.name,
            mode=row.mode,
            created_at=_as_utc(row.created_at),
            route=storage.get_channel_route(row.name),
        )
        for row in storage.list_channels(enabled_only=True)
    )

    def _baselined(artist_mbid: str) -> bool:
        return window_start <= baseline_at[artist_mbid] <= window_end

    observations: list[Observation] = []
    for group in groups:
        seen = _as_utc(group.first_seen_at)
        if not (window_start <= seen <= window_end):
            continue
        recorded = [
            kind
            for kind, _created in kinds_by_group.get(group.id or -1, [])
            if kind in ("new", "upcoming")
        ]
        observations.append(
            _observation(
                group,
                names,
                owners,
                observed_at=seen,
                counterfactual_kind=_counterfactual_kind(group),
                recorded_kind=recorded[0] if recorded else None,
                baselined_in_window=_baselined(group.artist_mbid),
            )
        )
    observations.extend(
        _date_change_observations(
            groups,
            kinds_by_group,
            names,
            owners,
            window_start=window_start,
            window_end=window_end,
            baselined=_baselined,
        )
    )
    observations.sort(key=lambda obs: (obs.observed_at, obs.release_group_mbid))
    span = (window_end.date() - window_start.date()).days + 1
    return (
        tuple(observations),
        channels,
        max(0, span - len({obs.observed_on for obs in observations})),
    )


def run_simulation(
    storage: Storage,
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    proposed_defaults: SettingsOverride | None = None,
) -> SimulationReport:
    """Build the corpus and replay it under a proposed global policy.

    ``proposed_defaults`` of ``None`` replays the policy currently in force,
    which is what the acceptance criterion checks: simulating today's policy
    has to reproduce the deliveries that were actually recorded.
    """
    observations, channels, quiet_days = build_corpus(storage, window_start, window_end)
    mbids = sorted({observation.artist_mbid for observation in observations})
    in_force = storage.effective_watch_settings_for_mbids(mbids)
    proposed = (
        in_force
        if proposed_defaults is None
        else storage.effective_watch_settings_for_mbids(mbids, proposed_defaults)
    )
    return simulate(
        observations,
        channels,
        proposed,
        in_force,
        window_start=window_start,
        window_end=window_end,
        days_without_observations=quiet_days,
    )


_OUTCOME_LABELS = {
    OUTCOME_DELIVERED: "would be delivered",
    OUTCOME_FILTERED: "filtered by release type",
    OUTCOME_MUTED: "suppressed — artist muted that day",
    OUTCOME_ROUTED_AWAY: "recorded, but no channel subscribes",
    OUTCOME_BASELINE: "not replayable — artist's first poll is in this window",
    OUTCOME_UNEXPLAINED: "unexplained — allowed and post-baseline, yet no event",
}


def _coverage_lines(report: SimulationReport) -> list[str]:
    """State what the window does not cover, in its own words.

    Every line here is a gap rather than a finding. A replay that printed only
    its totals would read as complete over a window it could only partly see.
    """
    span = (report.window_end.date() - report.window_start.date()).days + 1
    lines = [
        f"Based on {report.observations} recorded observation(s) between "
        f"{report.window_start.date().isoformat()} and "
        f"{report.window_end.date().isoformat()} ({span} day(s)).",
    ]
    if report.days_without_observations:
        lines.append(
            f"  {report.days_without_observations} day(s) in the window recorded nothing "
            f"at all. A day with no watch run is not a quiet day."
        )
    if report.artists_baselined_in_window:
        excluded = sum(1 for o in report.outcomes if o.outcome == OUTCOME_BASELINE)
        lines.append(
            f"  {len(report.artists_baselined_in_window)} artist(s) were first polled "
            f"inside this window, so {excluded} observation(s) are excluded: a back "
            f"catalogue is silent on the baseline poll under every policy (ADR-0011)."
        )
    unexplained = sum(1 for o in report.outcomes if o.outcome == OUTCOME_UNEXPLAINED)
    if unexplained:
        lines.append(
            f"  {unexplained} observation(s) are unexplained: the policy in force "
            f"allowed them and they are post-baseline, yet no event was recorded. "
            f"They are counted in neither direction."
        )
    return lines


def format_report(report: SimulationReport, *, changes: list[OutcomeChange] | None = None) -> str:
    """Render one replay for `encore settings simulate`."""
    lines = _coverage_lines(report)
    if report.observations == 0:
        lines.append("")
        lines.append("Nothing to simulate.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Outcomes:")
    counts = report.counts_by_outcome()
    for key, label in _OUTCOME_LABELS.items():
        if key in counts:
            lines.append(f"  {counts[key]:>5}  {label}")
    lines.append("")
    per_channel = report.deliveries_by_channel()
    if not per_channel:
        lines.append("Deliveries: no enabled channel, so nothing would be sent.")
    else:
        lines.append(f"Deliveries ({report.deliveries} total):")
        for name, count in sorted(per_channel.items()):
            lines.append(f"  {count:>5}  {name}")
    lines.extend(_breakdown_lines(report))
    if changes is not None:
        lines.extend(_diff_lines(changes))
    return "\n".join(lines)


def _breakdown_lines(report: SimulationReport) -> list[str]:
    """Render the by-type and noisiest-artist tables, when there are any."""
    lines: list[str] = []
    by_type = report.by_type()
    if by_type:
        lines.append("")
        lines.append("By release type:")
        lines.extend(
            f"  {count:>5}  {label}"
            for label, count in sorted(by_type.items(), key=lambda i: (-i[1], i[0]))
        )
    noisiest = report.noisiest()
    if noisiest:
        lines.append("")
        lines.append("Noisiest artists:")
        lines.extend(f"  {count:>5}  {name}" for name, count in noisiest)
    return lines


def _diff_lines(changes: list[OutcomeChange]) -> list[str]:
    """Render the --diff section: only what a policy change actually moves."""
    if not changes:
        return ["", "Diff: no observation changes outcome under this policy."]
    lines = ["", f"Diff ({len(changes)} observation(s) change outcome):"]
    lines.extend(
        f"  {change.observation.observed_on.isoformat()}  "
        f"{change.observation.artist_name} — {change.observation.title}: "
        f"{_OUTCOME_LABELS.get(change.before, change.before)} → "
        f"{_OUTCOME_LABELS.get(change.after, change.after)}"
        for change in changes
    )
    return lines


def report_payload(
    report: SimulationReport, *, changes: list[OutcomeChange] | None = None
) -> dict[str, object]:
    """Render the same replay as JSON, gaps included as first-class fields."""
    payload: dict[str, object] = {
        "window_start": report.window_start.isoformat(),
        "window_end": report.window_end.isoformat(),
        "observations": report.observations,
        "replayable": report.replayable,
        "outcomes": report.counts_by_outcome(),
        "deliveries": report.deliveries,
        "deliveries_by_channel": report.deliveries_by_channel(),
        "by_artist": report.by_artist(),
        "by_type": report.by_type(),
        "by_day": {day.isoformat(): count for day, count in sorted(report.by_day().items())},
        "noisiest": [{"artist": name, "deliveries": n} for name, n in report.noisiest()],
        "days_without_observations": report.days_without_observations,
        "artists_baselined_in_window": list(report.artists_baselined_in_window),
    }
    if changes is not None:
        payload["diff"] = [
            {
                "observed_on": change.observation.observed_on.isoformat(),
                "artist": change.observation.artist_name,
                "title": change.observation.title,
                "before": change.before,
                "after": change.after,
            }
            for change in changes
        ]
    return payload
