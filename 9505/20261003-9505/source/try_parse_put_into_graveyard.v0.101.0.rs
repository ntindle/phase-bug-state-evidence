fn try_parse_put_into_graveyard(
    subject: &TargetFilter,
    rest: &str,
) -> Option<(TriggerMode, TriggerDefinition)> {
    // Match the verb prefix: "is put into " or "are put into "
    let (after_verb, ()) = alt((
        value((), tag::<_, _, OracleError<'_>>("is put into ")),
        value((), tag("are put into ")),
    ))
    .parse(rest)
    .ok()?;

    let (after_gy, possessive) = parse_graveyard_possessive.parse(after_verb).ok()?;

    // Parse optional "from [zone]" clause
    let after_gy = after_gy.trim_start();
    let (origin, origin_zones, union_qualifier) = if let Ok((after_from, ())) =
        value((), tag::<_, _, OracleError<'_>>("from ")).parse(after_gy)
    {
        let after_from = after_from.trim_start();
        parse_graveyard_origin_union(after_from)?
    } else {
        // No "from" clause -- no origin restriction (any zone to graveyard).
        // Strict tail (mirrors the exile sibling): anything else here is
        // unmodeled — fail the arm rather than silently truncate.
        if !after_gy.trim().is_empty() {
            return None;
        }
        (None, Vec::new(), OriginUnionQualifier::Unqualified)
    };

    // CR 109.5 + CR 400.3: gate ONLY the two-member union on owner-qualifier
    // consistency with the destination possessive (shared predicate with the
    // batched path); singles keep pre-existing behavior.
    if origin_zones.len() == 2
        && !union_qualifier_consistent_with_destination(union_qualifier, possessive.clone())
    {
        return None;
    }

    let valid_card = match possessive.clone() {
        Some(ctrl) => Some(add_controller(subject.clone(), ctrl)),
        None => Some(subject.clone()),
    };
    let valid_target =
        possessive.map(|ctrl| TargetFilter::Typed(TypedFilter::default().controller(ctrl)));

    let mut def = make_base();
    def.mode = TriggerMode::ChangesZone;
    def.destination = Some(Zone::Graveyard);
    def.origin = origin;
    def.origin_zones = origin_zones;
    def.valid_card = valid_card;
    def.valid_target = valid_target;
    Some((TriggerMode::ChangesZone, def))
}

/// CR 109.5: Parse the graveyard possessive in "put into [possessive] graveyard".
/// Returns the controller scope of the graveyard's owner, or `None` for unowned
/// ("a graveyard"). Shared by `try_parse_put_into_graveyard` and the batched
/// "one or more" variant so the two parse paths stay in lockstep.
