let origin_matches = if !trigger.origin_zones.is_empty() {
        from.is_some_and(|zone| trigger.origin_zones.contains(&zone))
    } else if let Some(origin) = trigger.origin {
        from == &Some(origin)
    } else if matches!(trigger.mode, TriggerMode::LeavesBattlefield) {
        from == &Some(Zone::Battlefield)
    } else {
        true
    };
