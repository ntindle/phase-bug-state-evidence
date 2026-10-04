fn try_parse_one_or_more_put_into_exile_from(
    lower: &str,
) -> Option<(TriggerMode, TriggerDefinition)> {
    for prefix in [
        "whenever one or more cards are put into exile from ",
        "when one or more cards are put into exile from ",
    ] {
        let Ok((rest, ())) = value((), tag::<_, _, OracleError<'_>>(prefix)).parse(lower) else {
            continue;
        };
        let Ok((after_zones, zones)) = parse_disjunctive_zone_set(rest) else {
            continue;
        };
        // CR 603.1 + CR 603.2: an optional trailing " during your turn" is part of
        // the trigger's event/condition — it restricts the batched
        // trigger to the controller's own turn (Ketramose, the New Dawn — "…are
        // put into exile from graveyards and/or the battlefield during your
        // turn"). Peel it here rather than bailing, so the disjunctive origin
        // (graveyards + battlefield) is captured AND the own-turn gate applies —
        // otherwise the line falls through to a less-precise parser that drops
        // both (firing on any turn and on exile from any zone, e.g. hand).
        let (after_zones, only_during_your_turn) =
            match value((), tag::<_, _, OracleError<'_>>(" during your turn")).parse(after_zones) {
                Ok((tail, ())) => (tail, true),
                Err(_) => (after_zones, false),
            };
        // Any other non-empty trailing text is an unhandled constraint clause —
        // bail so another parser can try.
        if !after_zones.is_empty() {
            continue;
        }

        let mut def = make_base();
        def.mode = TriggerMode::ChangesZoneAll;
        def.origin_zones = zones;
        def.destination = Some(Zone::Exile);
        def.batched = true;
        if only_during_your_turn {
            def.constraint = Some(TriggerConstraint::OnlyDuringYourTurn);
        }
        // CR 113.6 / CR 113.6b: this batched "cards are put into exile from
        // library/graveyard" ability is a permanent's triggered ability whose source
        // (e.g. Laelia the Blade Reforged, Rakshasa Vizier) is on the battlefield, and
        // it doesn't state that it functions from any other zone — so it keeps
        // make_base()'s battlefield-only default. There is no self-referential subject
        // here (valid_card is None), so no graveyard/exile look-back zones are needed.
        return Some((TriggerMode::ChangesZoneAll, def));
    }

    None
}

