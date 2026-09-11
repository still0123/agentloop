"""Bounded literal event index. Labels describe text matches, never causality."""

import re

STAMP = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")
SERVICE = re.compile(r"\b[a-z][\w-]*(?:\.[a-z][\w-]*){2,}\b")
METRIC = re.compile(
    r"(?P<name>request_timeout|long time\s+Cost|Cost|duration|elapsed|latency)"
    r"\s*[:=]\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s)?",
    re.I,
)
SIGNALS = {
    "timeout": re.compile(r"(?<![\w])timeout\b|timed out|deadline exceeded", re.I),
    "conflict": re.compile(r"\b409\b|already.*processing|处理中", re.I),
    "failure": re.compile(
        r"\b(?:504|500|400)\b|InternalError|InstanceExpireTimeRequired|ExitErr|\bfailed\b",
        re.I,
    ),
    "completion": re.compile(
        r"\bsuccess(?:ful(?:ly)?)?\b|ExitSuccess|\bcompleted\b", re.I
    ),
    "duration": re.compile(
        r"(?:request_timeout|Cost|duration|elapsed|latency)\s*[:=]\s*\d+(?:\.\d+)?\s*(?:ms|s)?",
        re.I,
    ),
}


def index_log_text(text, query="", max_events=12):
    if not isinstance(query, str) or len(query) > 256 or "\n" in query:
        raise ValueError("query must be one bounded literal")
    if type(max_events) is not int or not 1 <= max_events <= 24:
        raise ValueError("max_events must be in 1..24")
    lines = text.splitlines(keepends=True)
    events, services, groups, metrics = [], {}, {}, {}
    for number, line in enumerate(lines, 1):
        if query and query not in line:
            continue
        header = line.split("_msg=", 1)[0][:500]
        timestamp = STAMP.search(header)
        service = SERVICE.search(header)
        service = service.group() if service else "unknown"
        services[service] = services.get(service, 0) + 1
        for match in METRIC.finditer(line):
            key = (service, match["name"].lower(), match["unit"])
            # Keep the largest observed value per exact metric/unit, without
            # conflating configured budgets with measured durations.
            value = float(match["value"])
            if key not in metrics or value > metrics[key]["value"]:
                metrics[key] = {
                    "service": service,
                    "time": timestamp.group() if timestamp else None,
                    "metric": match["name"],
                    "value": value,
                    "unit": match["unit"],
                    "line": number,
                    "char_offset": match.start(),
                    "text": match.group(),
                }

        hits = [(kind, pattern.search(line)) for kind, pattern in SIGNALS.items()]
        hits = [(kind, hit) for kind, hit in hits if hit]
        labels = [kind for kind, _ in hits]
        # Retain windows near actual signals, including the far end of huge bodies.
        centers = [hit.start() for _, hit in hits] or [
            line.find(query) if query else len(header)
        ]
        spans = []
        for center in centers:
            start, end = max(0, center - 70), min(len(line), center + 170)
            if any(a <= start and end <= b for a, b in spans):
                continue
            spans.append((start, end))
        event = {
            "line": number,
            "time": timestamp.group() if timestamp else None,
            "service": service,
            "signals": labels,
            "snippets": [{"char_offset": a, "text": line[a:b]} for a, b in spans[:3]],
        }
        events.append(event)
        for label in labels or ["other"]:
            group = groups.setdefault((service, label), [])
            if not group:
                group.append(len(events) - 1)
            elif len(group) == 1:
                group.append(len(events) - 1)
            else:
                group[-1] = len(events) - 1
    # First/last representatives of each service/signal before ordinary rows.
    picks = []
    queues = {label: [] for label in [*SIGNALS, "other"]}
    for (_, label), indices in groups.items():
        queues[label].extend(index for index in indices if index not in queues[label])
    # Round-robin across signal classes: many timeout services must not evict
    # every completion/duration event from the bounded index.
    while any(queues.values()) and len(picks) < max_events:
        for queue in queues.values():
            while queue and queue[0] in picks:
                queue.pop(0)
            if queue and len(picks) < max_events:
                picks.append(queue.pop(0))
    chosen = sorted(
        picks[:max_events], key=lambda i: (events[i]["time"] or "", events[i]["line"])
    )
    return {
        "total_lines": len(lines),
        "matched_lines": len(events),
        "selected_events": len(chosen),
        "omitted_events": len(events) - len(chosen),
        "services": dict(sorted(services.items(), key=lambda item: -item[1])[:24]),
        "omitted_services": max(0, len(services) - 24),
        "query": query,
        "events": [events[i] for i in chosen],
        "metrics": sorted(
            metrics.values(),
            key=lambda item: (
                "long time" not in item["metric"].lower(),
                item["metric"],
            ),
        )[:24],
        "omitted_metric_groups": max(0, len(metrics) - 24),
        "metric_scope": (
            "Largest literal value per service, metric and explicit unit within "
            "the selected export. Not an inferred duration of the target request; "
            "correlate its source line and time before use."
        ),
        "scope": (
            "Literal event index of this captured export. Snippets and signal "
            "labels are observations, not a causal conclusion. Omitted events "
            "and unexpanded line bodies remain available; indexing does not "
            "prove all branches completed."
        ),
    }
