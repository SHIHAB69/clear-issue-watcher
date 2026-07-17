"""Core loop: for each registered source, discover new events into its FIFO
queue, then drain serially (one event → resume that source's session → act →
pop only on success). Serial within and across sources: exactly one Claude
turn active at a time. Per-source lock guards against overlap.
"""
from . import config, runtime
from .adapters import build_adapter

MAX_ATTEMPTS = 5
COMPACT_EVERY = 25        # events per source before rolling the session into memory


def discover_into_queue(source: config.Source, adapter) -> int:
    state = source.state()
    since = state["last_checked"]
    processed = list(state["processed"])   # keep insertion order (trim is meaningful)
    seen = set(processed)
    found = []
    for ev in adapter.discover_events(since):
        key = f"{ev.kind}:{ev.external_id}"
        if key in seen:
            continue
        seen.add(key)
        processed.append(key)
        if adapter.is_self_event(ev):      # anti-loop
            continue
        found.append(ev)
    found.sort(key=lambda e: e.ts)         # FIFO by creation time
    for ev in found:
        source.enqueue(ev.to_dict())
    if found:
        config.log(f"[{source.slug}] queued {len(found)}: "
                   + ", ".join(f"{e.kind}#{e.external_id}" for e in found))
    state["processed"] = processed
    state["last_checked"] = config.now_iso()
    source.save_state(state)
    return len(found)


def enqueue_user_message(source: config.Source, text: str) -> None:
    """UI-sent operator message → a queued event, processed in order like an
    issue event (injected into the session as an operator message, not a comment)."""
    from .adapters.base import Event
    import time as _t
    ev = Event(source.slug, "user_message", f"msg-{int(_t.time()*1000)}",
               config.now_iso(), title="operator message", data={"text": text})
    source.enqueue(ev.to_dict())


def drain_queue(source: config.Source, adapter, interactive: bool = False,
                emit=None, ask=None) -> None:
    while True:
        q = source.queue()
        if not q:
            return
        event = q[0]
        event["attempts"] = event.get("attempts", 0) + 1
        source.lock()                      # refresh during long runs
        try:
            ok, _ = runtime.run_event(source, adapter, event, interactive=interactive,
                                      emit=emit, ask=ask)
        except Exception as e:  # noqa: BLE001 — a bad event must not crash the cycle
            config.log(f"[{source.slug}] run_event raised: {e}")
            ok = False
        if ok:
            source.write_queue(q[1:])
            # count handled events; compact the rolling session periodically
            st = source.state()
            st["events_since_compaction"] = st.get("events_since_compaction", 0) + 1
            if st["events_since_compaction"] >= COMPACT_EVERY:
                if runtime.compact(source, adapter):
                    st["events_since_compaction"] = 0
            source.save_state(st)
        elif event["attempts"] >= MAX_ATTEMPTS:
            config.log(f"[{source.slug}] GIVING UP {event.get('external_id')} "
                       f"after {event['attempts']} attempts")
            source.write_queue(q[1:])
        else:
            q[0] = event                   # persist attempt count
            source.write_queue(q)
            return                          # retry next cycle


def run_source(slug: str, interactive: bool = False, emit=None, ask=None,
               adapter=None, discover: bool = True) -> None:
    source = config.Source(slug)
    if not source.meta:
        config.log(f"[{slug}] no such source; skipping")
        return
    if source.paused():                    # human hit /stop — hold autonomous work
        return
    if source.locked():
        return
    source.lock()
    try:
        adapter = adapter or build_adapter(source.meta)
        if discover:
            discover_into_queue(source, adapter)
        drain_queue(source, adapter, interactive=interactive, emit=emit, ask=ask)
    except Exception as e:  # noqa: BLE001
        config.log(f"[{slug}] ERROR: {e}")
    finally:
        source.unlock()


def run_all() -> None:
    for s in config.load_config()["sources"]:
        run_source(s["slug"])
