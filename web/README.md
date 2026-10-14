# SI console

Next.js / TypeScript / Tailwind workspace for a running SI API. All API calls happen directly in the browser.

```sh
pnpm install --frozen-lockfile
pnpm dev
```

Open http://localhost:3000. Enter your API key in the connection bar; it is held only in page memory and cleared on reload. **Use local dev key** explicitly selects the local bootstrap credential, not a production default. Configure API CORS to allow your console origin.

`NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000`. It is a public, build-time URL reachable from the **browser**, not a server-internal Docker hostname. Never put a secret in a `NEXT_PUBLIC_*` variable. No server API credentials are read or forwarded.

The console includes streaming model chat, agent editing and background runs with approval, knowledge collection creation/document upload/retrieval, execution traces, and dataset evaluations. Data comes from the API; there are no seeded display records.

## Validation

```sh
pnpm lint
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
pnpm test
```

Browser tests start a production server on port 3219 and mock the API. They cover all sections, error states, Unicode SSE chunk boundaries, and responsive layout. Screenshots and failure traces are saved under ignored `test-results/`; the HTML report is in `playwright-report/`.

## Container

```sh
docker build --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 -t si-console .
docker run --rm -p 3000:3000 si-console
```

The standalone image runs as a non-root user. Changing the API URL requires rebuilding.

`pnpm audit --prod` is clean. The scaffold's lint-only dependency chain currently includes `braces@3.0.3` (GHSA-vfj7-8cjw-p6xm), with no published patched release; it is not included in the standalone production runtime.
