# Security Policy

## Supported Versions

This repository is maintained on the `main` branch. Security fixes are applied to the latest public version unless a release branch explicitly states otherwise.

## Reporting a Vulnerability

If you believe you have found a vulnerability, please report it through GitHub Security Advisories when available, or open a minimal issue that does not include exploit details.

A useful report includes:

- affected version or commit;
- operating system and runtime details;
- steps to reproduce;
- expected and observed behavior;
- impact assessment;
- any safe proof-of-concept details that help reproduce the issue.

Please avoid publishing working exploits, credentials, private logs, or sensitive runtime data in public issues.

## Disclosure and Response

I will review valid reports, confirm reproducibility when possible, and publish a fix or mitigation note. For issues that affect downstream users, coordinated disclosure is preferred so users have time to update.

## Scope

In scope:

- vulnerabilities in the plugin code contained in this repository;
- unsafe defaults that could expose local services or files;
- dependency or configuration issues introduced by this project.

Out of scope:

- vulnerabilities in third-party services or browsers themselves;
- issues that require already-compromised local access without increasing impact;
- social-engineering reports without a technical vulnerability in this codebase.
