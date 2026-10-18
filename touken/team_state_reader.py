"""Team observation boundary; gameplay may supply another client reader."""
import subprocess
import time

from .team_state import TeamSnapshot


def read_team_snapshot(agent, team_no, *, cache=None, max_age=600, now=None,
                       default_connection=False):
    """Read once per decision. An injected reader returns a fresh TeamSnapshot.

    ``agent.team_state_reader(team_no, max_age=..., now=...)`` replaces the
    default client adapter completely; its failure never falls through to it.
    A caller-owned cache lasts one safety chain, never a whole battle run.
    """
    cache = {} if cache is None else cache
    now = time.time() if now is None else now
    provider = getattr(agent, "team_state_reader", None)
    try:
        if provider is not None:
            if not callable(provider):
                return None
            snapshots = cache.setdefault("team_snapshots", {})
            key = (team_no, max_age, id(getattr(provider, "__self__", None)),
                   id(getattr(provider, "__func__", provider)))
            if key not in snapshots:
                snapshots[key] = provider(team_no, max_age=max_age, now=now)
            snapshot = snapshots[key]
        else:
            from . import game_state
            maa = getattr(agent, "maa", None)
            if maa is None:
                return None
            if "events" not in cache:
                if default_connection:
                    from .youzu_log import DEFAULT_ADB, DEFAULT_ADDRESS
                    cache["events"] = game_state.read_events(
                        getattr(maa, "adb_path", "") or DEFAULT_ADB,
                        getattr(maa, "adb_address", "") or DEFAULT_ADDRESS)
                else:
                    cache["events"] = game_state.available_events(maa)
            snapshot = game_state.team_snapshot(cache["events"], team_no, max_age=max_age, now=now)
        if not isinstance(snapshot, TeamSnapshot) or snapshot.team_no != team_no:
            return None
        return snapshot.current(now=now, max_age=max_age)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return None
