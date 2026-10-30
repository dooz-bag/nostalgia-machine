# Security and safety

This project is not a network service in the usual sense — it has no accounts, no
authentication and no database of users. The risks that matter are different, and
worth stating plainly.

## Please do not report media

The repository ships **no** shows, bumps, commercials or music, and it never
downloads any. If you believe a file in this repository infringes a copyright,
open an issue naming the file and it will be removed.

Do not attach media to an issue or a pull request. Log excerpts that name a file are
fine and useful; the file itself is not.

## If you expose an instance to the internet

`nostalgia serve` and the streamer listen without authentication. The settings page
can rewrite configuration and the scheduler can rebuild the channel, so an instance
published on a public address is an open administrative interface. If you want to
reach it from outside your own network, put it behind something that authenticates
(VPN, reverse proxy with auth) rather than forwarding the port.

`docker-compose.yml` publishes the web UI and the streamer on all interfaces by
design — the whole point is a television on your LAN reaching them.

## Reporting an actual vulnerability

For a genuine software vulnerability — a parser crash reachable from a config file,
a path-traversal in the media scanner, a dependency advisory — open an issue with
the version, the platform and the smallest reproduction you can manage. If the
report is sensitive, say so in the issue and a private channel can be arranged;
please do not publish a working exploit before then.

## Supported versions

The default branch is what is supported. This is a personal project rather than a
released product, so fixes land on the branch rather than in a versioned release.
