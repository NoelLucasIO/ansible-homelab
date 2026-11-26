-- Tideline schema. Idempotent: deploy.yml runs it on every deploy, as the owning role.
-- The collectors' JSON goes in through tideline.ingest(); Grafana reads with a SELECT-only role.
CREATE SCHEMA IF NOT EXISTS tideline;

-- One row per system: Linux host, firewall or appliance.
CREATE TABLE IF NOT EXISTS tideline.hosts (
  host            text PRIMARY KEY,
  kind            text NOT NULL DEFAULT 'linux',          -- linux | firewall | appliance
  os              text,
  os_version      text,
  kernel          text,
  managers        text[] NOT NULL DEFAULT '{}',
  reboot_required boolean,
  last_attempt    timestamptz,
  last_success    timestamptz,
  last_errors     jsonb NOT NULL DEFAULT '[]'
);

-- Every update ever seen. Open while resolved_at IS NULL; resolved when a later successful
-- run of the same source no longer offers it (installed, or withdrawn). One open row per
-- (host, source, name, arch): a newer candidate replaces new_version but keeps first_seen,
-- so "pending since" is when the package first had an update waiting.
CREATE TABLE IF NOT EXISTS tideline.updates (
  id                bigserial PRIMARY KEY,
  host              text NOT NULL REFERENCES tideline.hosts (host) ON DELETE CASCADE,
  source            text NOT NULL,                        -- dnf pacman aur apt flatpak fwupd container opnsense appliance
  name              text NOT NULL,
  arch              text NOT NULL DEFAULT '',             -- containers: the user running them
  installed_version text,
  installed_at      timestamptz,
  new_version       text,
  repo              text,
  summary           text,
  security          boolean NOT NULL DEFAULT false,
  severity          text,                                 -- critical | high | medium | low | NULL
  advisory_id       text,
  advisory_type     text,                                 -- security bugfix enhancement newpackage | major minor patch rebuild | firmware
  advisory_title    text,
  advisory_text     text,
  advisory_url      text,
  advisory_issued   timestamptz,
  advisories        jsonb NOT NULL DEFAULT '[]',          -- every advisory covering the jump
  cves              text[] NOT NULL DEFAULT '{}',
  package_url       text,
  changes_url       text,
  first_seen        timestamptz NOT NULL DEFAULT now(),
  last_seen         timestamptz NOT NULL DEFAULT now(),
  resolved_at       timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS updates_open_key ON tideline.updates (host, source, name, arch) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS updates_resolved ON tideline.updates (resolved_at) WHERE resolved_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS updates_first_seen ON tideline.updates (first_seen);

-- Vendor appliances: the catalog you keep (tideline_appliances) plus what a probe read live.
CREATE TABLE IF NOT EXISTS tideline.appliances (
  name              text PRIMARY KEY,
  product           text,
  vendor            text,
  installed_version text,
  installed_source  text,                                 -- live | manual
  installed_on      date,
  latest_version    text,
  latest_released   date,
  update_available  boolean,
  release_notes_url text,
  download_url      text,
  notes             text,
  probe             text,
  probe_error       text,
  checked_at        timestamptz
);

-- p: {"hosts": [collector output + {"host", "kind", "ok"}], "appliances": [...], "keep_days": n}
CREATE OR REPLACE FUNCTION tideline.ingest(p jsonb) RETURNS text
LANGUAGE plpgsql AS $fn$
DECLARE
  h        jsonb;
  srcs     text[];
  n_hosts  int := 0;
  n_open   int := 0;
  n_res    int := 0;
  k        int;
BEGIN
  FOR h IN SELECT * FROM jsonb_array_elements(coalesce(p->'hosts', '[]')) LOOP
    n_hosts := n_hosts + 1;
    INSERT INTO tideline.hosts AS t (host, kind, os, os_version, kernel, managers, reboot_required,
                                     last_attempt, last_success, last_errors)
    VALUES (h->>'host', coalesce(h->>'kind', 'linux'), h->>'os', h->>'os_version', h->>'kernel',
            ARRAY(SELECT jsonb_array_elements_text(coalesce(h->'managers', '[]'))),
            (h->>'reboot_required')::boolean, now(),
            CASE WHEN (h->>'ok')::boolean THEN now() END, coalesce(h->'errors', '[]'))
    ON CONFLICT (host) DO UPDATE SET
      kind = EXCLUDED.kind,
      os = coalesce(EXCLUDED.os, t.os),
      os_version = coalesce(EXCLUDED.os_version, t.os_version),
      kernel = coalesce(EXCLUDED.kernel, t.kernel),
      managers = CASE WHEN cardinality(EXCLUDED.managers) > 0 THEN EXCLUDED.managers ELSE t.managers END,
      reboot_required = coalesce(EXCLUDED.reboot_required, t.reboot_required),
      last_attempt = EXCLUDED.last_attempt,
      last_success = coalesce(EXCLUDED.last_success, t.last_success),
      last_errors = EXCLUDED.last_errors;

    -- Unreachable host: keep what we knew. A source that failed: keep its rows as they were.
    CONTINUE WHEN NOT coalesce((h->>'ok')::boolean, false);
    srcs := ARRAY(SELECT jsonb_array_elements_text(coalesce(h->'sources_ok', '[]')));

    INSERT INTO tideline.updates AS u (host, source, name, arch, installed_version, installed_at,
        new_version, repo, summary, security, severity, advisory_id, advisory_type, advisory_title,
        advisory_text, advisory_url, advisory_issued, advisories, cves, package_url, changes_url)
    SELECT DISTINCT ON (x.source, x.name, coalesce(x.arch, ''))
           h->>'host', x.source, x.name, coalesce(x.arch, ''), x.installed_version,
           to_timestamp(x.installed_at), x.new_version, x.repo, x.summary, coalesce(x.security, false),
           x.severity, x.advisory_id, x.advisory_type, x.advisory_title, x.advisory_text, x.advisory_url,
           to_timestamp(x.advisory_issued), coalesce(x.advisories, '[]'),
           ARRAY(SELECT jsonb_array_elements_text(coalesce(x.cves, '[]'))), x.package_url, x.changes_url
      FROM jsonb_to_recordset(coalesce(h->'updates', '[]')) AS x(
             source text, name text, arch text, installed_version text, installed_at double precision,
             new_version text, repo text, summary text, security boolean, severity text,
             advisory_id text, advisory_type text, advisory_title text, advisory_text text,
             advisory_url text, advisory_issued double precision, advisories jsonb, cves jsonb,
             package_url text, changes_url text)
     WHERE x.source = ANY (srcs)
     ORDER BY x.source, x.name, coalesce(x.arch, '')
    ON CONFLICT (host, source, name, arch) WHERE resolved_at IS NULL DO UPDATE SET
      installed_version = EXCLUDED.installed_version, installed_at = EXCLUDED.installed_at,
      new_version = EXCLUDED.new_version, repo = EXCLUDED.repo, summary = EXCLUDED.summary,
      security = EXCLUDED.security, severity = EXCLUDED.severity, advisory_id = EXCLUDED.advisory_id,
      advisory_type = EXCLUDED.advisory_type, advisory_title = EXCLUDED.advisory_title,
      advisory_text = EXCLUDED.advisory_text, advisory_url = EXCLUDED.advisory_url,
      advisory_issued = EXCLUDED.advisory_issued, advisories = EXCLUDED.advisories, cves = EXCLUDED.cves,
      package_url = EXCLUDED.package_url, changes_url = EXCLUDED.changes_url, last_seen = now();
    GET DIAGNOSTICS k = ROW_COUNT;
    n_open := n_open + k;

    UPDATE tideline.updates u SET resolved_at = now()
     WHERE u.host = h->>'host' AND u.resolved_at IS NULL AND u.source = ANY (srcs)
       AND NOT EXISTS (
             SELECT 1 FROM jsonb_to_recordset(coalesce(h->'updates', '[]')) AS x(source text, name text, arch text)
              WHERE x.source = u.source AND x.name = u.name AND coalesce(x.arch, '') = u.arch);
    GET DIAGNOSTICS k = ROW_COUNT;
    n_res := n_res + k;
  END LOOP;

  INSERT INTO tideline.appliances AS a (name, product, vendor, installed_version, installed_source,
      installed_on, latest_version, latest_released, update_available, release_notes_url, download_url,
      notes, probe, probe_error, checked_at)
  SELECT x.name, x.product, x.vendor, x.installed_version, x.installed_source, x.installed_on,
         x.latest_version, x.latest_released, x.update_available, x.release_notes_url, x.download_url,
         x.notes, x.probe, x.probe_error, now()
    FROM jsonb_to_recordset(coalesce(p->'appliances', '[]')) AS x(
           name text, product text, vendor text, installed_version text, installed_source text,
           installed_on date, latest_version text, latest_released date, update_available boolean,
           release_notes_url text, download_url text, notes text, probe text, probe_error text)
  ON CONFLICT (name) DO UPDATE SET
    product = EXCLUDED.product, vendor = EXCLUDED.vendor, installed_version = EXCLUDED.installed_version,
    installed_source = EXCLUDED.installed_source, installed_on = EXCLUDED.installed_on,
    latest_version = EXCLUDED.latest_version, latest_released = EXCLUDED.latest_released,
    update_available = EXCLUDED.update_available, release_notes_url = EXCLUDED.release_notes_url,
    download_url = EXCLUDED.download_url, notes = EXCLUDED.notes, probe = EXCLUDED.probe,
    probe_error = EXCLUDED.probe_error, checked_at = EXCLUDED.checked_at;

  -- Appliances removed from the catalog leave the dashboard (only when a catalog was sent).
  IF p ? 'appliances' THEN
    DELETE FROM tideline.appliances
     WHERE name NOT IN (SELECT x->>'name' FROM jsonb_array_elements(p->'appliances') x);
  END IF;

  DELETE FROM tideline.updates
   WHERE resolved_at < now() - make_interval(days => coalesce((p->>'keep_days')::int, 365));

  RETURN format('hosts=%s open_upserts=%s resolved=%s', n_hosts, n_open, n_res);
END
$fn$;

-- Prometheus exposition text for node_exporter's textfile collector, from the database's
-- current state (so the OPNsense job and the Linux job both rewrite one consistent file).
CREATE OR REPLACE FUNCTION tideline.metrics() RETURNS text
LANGUAGE sql STABLE AS $fn$
WITH open AS (SELECT * FROM tideline.updates WHERE resolved_at IS NULL),
lines AS (
  SELECT 1 AS o, '# HELP tideline_updates_pending Updates waiting, by host and source.' AS l
  UNION ALL SELECT 2, '# TYPE tideline_updates_pending gauge'
  UNION ALL SELECT 3, format('tideline_updates_pending{host="%s",kind="%s",source="%s"} %s', h.host, h.kind, s.source, count(o.id))
    FROM tideline.hosts h
    CROSS JOIN LATERAL (SELECT DISTINCT source FROM tideline.updates WHERE host = h.host
                        UNION SELECT 'none') s
    LEFT JOIN open o ON o.host = h.host AND o.source = s.source
   GROUP BY h.host, h.kind, s.source
  UNION ALL SELECT 4, '# HELP tideline_security_pending Security updates waiting, by host and severity.'
  UNION ALL SELECT 5, '# TYPE tideline_security_pending gauge'
  UNION ALL SELECT 6, format('tideline_security_pending{host="%s",severity="%s"} %s', h.host, sv.severity,
                             count(o.id))
    FROM tideline.hosts h
    CROSS JOIN (VALUES ('critical'), ('high'), ('medium'), ('low'), ('unknown')) sv(severity)
    LEFT JOIN open o ON o.host = h.host AND o.security AND coalesce(o.severity, 'unknown') = sv.severity
   GROUP BY h.host, sv.severity
  UNION ALL SELECT 7, '# HELP tideline_security_oldest_timestamp_seconds When the longest-waiting security update of that severity first appeared (absent when none).'
  UNION ALL SELECT 8, '# TYPE tideline_security_oldest_timestamp_seconds gauge'
  UNION ALL SELECT 9, format('tideline_security_oldest_timestamp_seconds{host="%s",severity="%s"} %s', o.host,
                             coalesce(o.severity, 'unknown'), extract(epoch FROM min(o.first_seen))::bigint)
    FROM open o WHERE o.security GROUP BY o.host, coalesce(o.severity, 'unknown')
  UNION ALL SELECT 10, '# HELP tideline_reboot_required 1 when the host runs an older kernel/libraries than installed.'
  UNION ALL SELECT 11, '# TYPE tideline_reboot_required gauge'
  UNION ALL SELECT 12, format('tideline_reboot_required{host="%s"} %s', host, CASE WHEN reboot_required THEN 1 ELSE 0 END)
    FROM tideline.hosts WHERE reboot_required IS NOT NULL
  UNION ALL SELECT 13, '# HELP tideline_last_success_timestamp_seconds Last complete collection from the host.'
  UNION ALL SELECT 14, '# TYPE tideline_last_success_timestamp_seconds gauge'
  UNION ALL SELECT 15, format('tideline_last_success_timestamp_seconds{host="%s",kind="%s"} %s', host, kind,
                              coalesce(extract(epoch FROM last_success)::bigint, 0))     -- 0: never, so the stale alert fires
    FROM tideline.hosts
  UNION ALL SELECT 16, '# HELP tideline_collect_errors Errors in the host''s last collection (some sources may be missing).'
  UNION ALL SELECT 17, '# TYPE tideline_collect_errors gauge'
  UNION ALL SELECT 18, format('tideline_collect_errors{host="%s"} %s', host, jsonb_array_length(last_errors))
    FROM tideline.hosts
  UNION ALL SELECT 19, '# HELP tideline_appliance_update_available 1 when the catalog''s latest version is newer than what runs.'
  UNION ALL SELECT 20, '# TYPE tideline_appliance_update_available gauge'
  UNION ALL SELECT 21, format('tideline_appliance_update_available{appliance="%s"} %s', replace(name, '"', ''),
                              CASE WHEN update_available THEN 1 ELSE 0 END)
    FROM tideline.appliances WHERE update_available IS NOT NULL
)
SELECT string_agg(l, E'\n' ORDER BY o, l) || E'\n' FROM lines;
$fn$;
