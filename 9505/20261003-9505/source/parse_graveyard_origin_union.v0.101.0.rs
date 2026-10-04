fn parse_graveyard_origin_union(
    input: &str,
) -> Option<(Option<Zone>, Vec<Zone>, OriginUnionQualifier)> {
    // Bare "anywhere" single: explicitly unconstrained (CR 603.6c: an ability
    // that triggers on a card put into a zone "from anywhere" is never a
    // leaves-the-battlefield ability). Strict tail — "anywhere other than X"
    // belongs to the zone-change-clause path, not here.
    if let Ok((tail, _)) = tag::<_, _, OracleError<'_>>("anywhere").parse(input) {
        return tail.trim().is_empty().then_some((
            None,
            Vec::new(),
            OriginUnionQualifier::Unqualified,
        ));
    }
    let (tail, members) = parse_qualified_graveyard_origin_pair(input).ok()?;
    if !tail.trim().is_empty() {
        return None;
    }
    match members.as_slice() {
        // A single member is trivially uniform; callers ignore the qualifier
        // for singles (pre-existing single-path behavior is out of scope).
        [(single, qualifier)] => Some((
            Some(*single),
            Vec::new(),
            resolve_origin_union_qualifier(*qualifier, *qualifier),
        )),
        [(head_zone, head), (second_zone, second)] => Some((
            None,
            vec![*head_zone, *second_zone],
            resolve_origin_union_qualifier(*head, *second),
        )),
        _ => None,
    }
}

/// CR 400.3: Shared parser for possessive hand forms in zone-change triggers.
/// Recognises "your hand", "an opponent's hand", "its owner's hand",
/// "their owner's hand", "their owners' hands", "a player's hand", "a hand",
/// and bare "hand". Returns `Some(controller)` when the possessive constrains
/// the destination owner, `None` when any player's hand matches.
