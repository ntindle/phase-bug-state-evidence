fn try_parse_put_into_exile_from(
    subject: &TargetFilter,
    rest: &str,
) -> Option<(TriggerMode, TriggerDefinition)> {
    let (after_verb, ()) = alt((
        value((), tag::<_, _, OracleError<'_>>("is put into exile")),
        value((), tag("are put into exile")),
    ))
    .parse(rest)
    .ok()?;

    let after_verb = after_verb.trim_start();
    let origin = if let Ok((after_from, ())) =
        value((), tag::<_, _, OracleError<'_>>("from ")).parse(after_verb)
    {
        let after_from = after_from.trim_start();
        fn parse_origin_zone(input: &str) -> OracleResult<'_, Option<Zone>> {
            alt((
                value(Some(Zone::Battlefield), tag("the battlefield")),
                value(None, tag("anywhere")),
                value(Some(Zone::Library), tag("your library")),
                value(Some(Zone::Hand), tag("your hand")),
                value(Some(Zone::Graveyard), tag("your graveyard")),
            ))
            .parse(input)
        }
        parse_origin_zone
            .parse(after_from)
            .ok()
            .map(|(_, z)| z)
            .unwrap_or(None)
    } else if after_verb.is_empty() {
        None
    } else {
        // Unknown trailing text — bail rather than silently truncate.
        return None;
    };

    let mut def = make_base();
    def.mode = TriggerMode::ChangesZone;
    def.destination = Some(Zone::Exile);
    def.origin = origin;
    def.valid_card = Some(subject.clone());
    if filter_references_self(subject) {
        def.trigger_zones = vec![Zone::Battlefield, Zone::Graveyard, Zone::Exile];
    }
    Some((TriggerMode::ChangesZone, def))
}

/// Parse "whenever one or more [type] cards are put into [your] graveyard from [your library]".
/// CR 603.2c: "One or more" triggers fire once per batch of simultaneous events.
