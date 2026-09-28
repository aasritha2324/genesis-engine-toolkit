<!-- LOVABLE:BEGIN -->
> [!IMPORTANT]
> This project is connected to [Lovable](https://lovable.dev). Avoid rewriting
> published git history — force pushing, or rebasing/amending/squashing commits
> that are already pushed — as it rewrites history on Lovable's side and the
> user will likely lose their project history.
>
> Commits you push to the connected branch sync back to Lovable and show up in
> the editor, so keep the branch in a working state.
<!-- LOVABLE:END -->

- Use Lovable Cloud for durable advertiser data and auth, with tenant ownership enforced by row policies; this preview runs in a serverless environment rather than Docker.
- Keep privileged click ingestion in authenticated server handlers or key-verified public HTTP handlers; a transactional database function handles event deduplication and minute-bucket updates.
- Store user roles separately from profiles; administrator access is only granted by a trusted operator, never during self-registration.
- Keep visual tokens in src/styles.css and the analytics shell at /; this preserves a consistent interface while the app stays previewable.
