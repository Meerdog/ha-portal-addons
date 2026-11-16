# Changelog

## 0.1.0
- First version: syncs the content repo, home page of cards, single and multi-file pages,
  hidden pages with unlisted links, versioned engines under /lib, sandboxing security policy,
  commit statuses, optional GitHub webhook.

## 0.1.1
- Errors are logged with details and answered with a message instead of a dropped connection.
- Logs its settings (token hidden) at start.

## 0.1.2
- Logs every incoming connection and runs a self-test at start, to trace connection problems.

## 0.2.0
- Share links: a second listener (port 8098) serves `shares/` from the content repo at
  `/s/<token>/`, without sign-in, until each share's end date. Expired links answer 410.
  Serves only the engine versions live shares use, sends no-store and noindex headers, and
  knows nothing else about the portal.
- Shared rules version 2.
