        if (cancelled) return;
        if (state) {
          const publicState = persistedGameStateView(state);
          setMatch(saved);
          // CR 800.4: eliminated players are out — only live seats count as
          // opponents. Seat 0 is the local human in AI/host matches.
          const you = publicState.players.find((p) => p.id === 0);
          const liveCount = publicState.players.filter((p) => !p.is_eliminated).length;
          setMatchSummary({
            turn: publicState.turn_number,
            isYourTurn: publicState.active_player === 0,
            yourLife: you && !you.is_eliminated ? you.life : null,
            opponentCount: Math.max(0, liveCount - 1),
          });
        } else {
          clearActiveGame();
