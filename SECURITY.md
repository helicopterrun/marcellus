# Security policy

Marcellus is maintained by one person in spare time. Reports are read and
taken seriously, but there is no formal SLA.

## Supported versions

Only the latest minor release train receives fixes (currently 0.4.x, image
tag `:0.4`). Older trains are not patched; please upgrade.

## Reporting a vulnerability

Please report privately, not in a public issue:

1. Go to the repository's **Security** tab and choose **Report a vulnerability**
   (GitHub private vulnerability reporting).
2. Describe what you found, the version or commit, and how to reproduce it.

Expect an acknowledgement within a few days, usually sooner. A fix and
coordinated disclosure timeline will be worked out in the advisory thread;
the aim is a fix in the next release, but this is best effort.

## Scope

In scope: the Marcellus server code in this repository, including its auth
handling and Frigate proxy, the push and APNs pipeline, the container image
and `install.sh`, and the web pages it serves.

Out of scope: vulnerabilities in Frigate, go2rtc, Docker, or other
dependencies (report those upstream), findings that need an attacker who
already has admin access to the host or the config file, and deployments
exposed to the internet without the access controls the deployment guide
recommends.
