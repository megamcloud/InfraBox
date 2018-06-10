import json
import select

import urllib
import requests
import psycopg2

from pyinfraboxutils import get_logger, get_env
from pyinfraboxutils.db import connect_db

logger = get_logger("github")

def execute_sql(conn, stmt, params): # pragma: no cover
    c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    c.execute(stmt, params)
    result = c.fetchall()
    c.close()
    return result

def main(): # pragma: no cover
    get_env('INFRABOX_VERSION')
    get_env('INFRABOX_DATABASE_DB')
    get_env('INFRABOX_DATABASE_USER')
    get_env('INFRABOX_DATABASE_PASSWORD')
    get_env('INFRABOX_DATABASE_HOST')
    get_env('INFRABOX_DATABASE_PORT')

    conn = connect_db()
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    logger.info("Connected to database")

    curs = conn.cursor()
    curs.execute("LISTEN job_update;")

    logger.info("Waiting for job updates")

    while True:
        if select.select([conn], [], [], 5) != ([], [], []):
            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                handle_job_update(conn, json.loads(notify.payload))


def handle_job_update(conn, event):
    job_id = event['job_id']

    jobs = execute_sql(conn, '''
        SELECT id, state, name, project_id, build_id
        FROM job
        WHERE id = %s
    ''', [job_id])

    if not jobs:
        return False

    job = jobs[0]

    project_id = job['project_id']
    build_id = job['build_id']

    projects = execute_sql(conn, '''
        SELECT id, name, type
        FROM project
        WHERE id = %s
    ''', [project_id])

    if not projects:
        return False

    project = projects[0]

    if project['type'] != 'github':
        return False

    builds = execute_sql(conn, '''
        SELECT id, build_number, restart_counter, commit_id
        FROM build
        WHERE id = %s
    ''', [build_id])

    if not builds:
        return False

    build = builds[0]

    project_name = project['name']
    job_state = job['state']
    job_name = job['name']
    commit_sha = build['commit_id']
    build_id = build['id']
    build_number = build['build_number']
    build_restartCounter = build['restart_counter']

    dashboard_url = execute_sql(conn, '''
        SELECT root_url
        FROM cluster
        WHERE name = 'master'
    ''', [])[0]['root_url']

    # determine github commit state
    state = 'success'
    if job_state in ('scheduled', 'running', 'queued'):
        state = 'pending'

    if job_state in ('failure', 'skipped', 'killed'):
        state = 'failure'

    if job_state == 'error':
        state = 'error'

    logger.info("")
    logger.info("Handle job %s", job_id)
    logger.info("Setting state to %s", state)

    token = execute_sql(conn, '''
        SELECT github_api_token FROM "user" u
        INNER JOIN collaborator co
            ON co.owner = true
            AND co.project_id = %s
            AND co.user_id = u.id
    ''', [project_id])

    if not token:
        logger.warn("No API token, not updating status")
        return False

    github_api_token = token[0]['github_api_token']

    github_status_url = execute_sql(conn, '''
        SELECT github_status_url
        FROM "commit"
        WHERE id = %s
        AND project_id = %s
    ''', [commit_sha, project_id])[0]['github_status_url']
    target_url = '%s/dashboard/#/project/%s/build/%s/%s/job/%s' % (dashboard_url,
                                                                   project_name,
                                                                   build_number,
                                                                   build_restartCounter,
                                                                   urllib.quote_plus(job_name).replace('+', '%20'))

    payload = {
        "state": state,
        "target_url": target_url,
        "description": "InfraBox",
        "context": "Job: %s" % job_name
    }

    headers = {
        "Authorization": "token " + github_api_token,
        "User-Agent": "InfraBox"
    }

    # TODO(ib-steffen): support ca bundles
    try:
        r = requests.post(github_status_url,
                          data=json.dumps(payload),
                          headers=headers,
                          timeout=10,
                          verify=False)

        if r.status_code != 201:
            logger.warn("Failed to update github status: %s", r.text)
            logger.warn(github_status_url)
        else:
            logger.info("Successfully updated github status")
    except Exception as e:
        logger.warn("Failed to update github status: %s", e)
        return False

    return True

if __name__ == "__main__": # pragma: no cover
    main()
