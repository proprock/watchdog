# Weekly agent-efficiency review

Run weekly or per milestone.

1. Refresh the local database:

   ```bash
   uv run wdi import
   ```

2. Check quantitative totals, priced coverage, and error-rate outliers:

   ```bash
   uv run wdi stats
   ```

3. Run qualitative analysis for new sessions:

   ```bash
   uv run wdi analyze --type prompt-quality --limit 10 --yes
   uv run wdi analyze --type agent-efficiency --limit 10 --yes
   ```

4. Compare with the previous review:
   token totals, tool error rates, semantic-vs-text discovery ratio, and repeated failing commands.

5. Write a short delta note.
   Update `GOTCHAS.md` only for confirmed reusable findings.

6. Check for new unpriced model ids.
   Extend `~/.wdi/config.yaml` pricing when `wdi stats` reports unpriced models.
