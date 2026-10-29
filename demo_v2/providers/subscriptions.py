"""Metadata-driven rollover and subscription demand; no time-bucket guessing."""

from datetime import datetime


def current_instances(instances, now: datetime):
    grouped = {}
    for instance in instances:
        if instance.verified and instance.status == "OPEN" and instance.starts_at <= now < instance.ends_at:
            grouped.setdefault(instance.definition.key,[]).append(instance)
    # An overlapping family has no authoritative current instance.
    return {key:values[0] for key,values in grouped.items() if len(values)==1}


def subscription_demand(instances, rules, positions, now, eligible):
    by_id = {instance.instance_id:instance for instance in instances}
    wanted = {}
    for rule in rules:
        spec = rule["spec"]
        if not spec.enabled or spec.archived:
            continue
        for definition, instance in current_instances(instances,now).items():
            if definition in spec.markets and eligible(rule,instance,now):
                wanted[instance.instance_id] = instance
    for position in positions:
        if position["status"] == "OPEN" and position["instance_id"] in by_id:
            instance = by_id[position["instance_id"]]
            if position["token_id"] in (instance.up_token_id,instance.down_token_id):
                wanted[instance.instance_id] = instance
    return tuple(wanted.values())
