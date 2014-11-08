from fabric.api import task, env, execute, settings
from fabric.operations import run, local, prompt
from fabric.utils import abort
from copy import copy, deepcopy
from boto.s3.connection import S3Connection
from boto.s3.key import Key
from datetime import datetime, timedelta
from time import time

@task
def files(dst='local', opts_string=''):
    """
    Syncs files from the environment's S3 bucket to `dst` env.files_path.
    """
    if dst == 'production':
        abort('Cannot sync to production.')
    if not 's3_files_bucket' in env:
        abort('Please configure an env.s3_files_bucket for this project.')

    execute(dst)
    dst_env = copy(env)

    opts_string += ' --region=us-west-2'

    if not 'files_path' in dst_env:
        abort('`files_path` not found in dst env.')

    if dst == 'local':
        # Ensure drupal.sync works anywhere in project structure by getting the
        # directory that the fabfile is in (project root).
        import os
        dst_files = os.path.dirname(env.real_fabfile) + '/' + dst_env.files_path
        local('aws s3 sync %s %s %s' % (env.s3_files_bucket,
            dst_files, opts_string));
    else:
        with settings(host_string=dst_env.hosts[0]):
            dst_files = '%s/%s' % (dst_env.host_site_path, dst_env.files_path)
            run('aws s3 sync %s %s %s' % (env.s3_files_bucket,
                dst_files, opts_string));

    print('+ Files synced to %s' % dst_env.files_path)

@task
def db(dst='local', opts_string=''):
    """
    Copies a database from `src` S3 bucket to `dst` environment.
    """
    global env
    src = env.tasks[0]

    if src == 'local':
        abort('Cannot sync from local.')
    if dst == 'production':
      force_push = prompt('Are you sure you want to push to production (WARNING: this will destroy production db):', None, 'n', 'y|n')
      if force_push == 'n':
        abort('Sync aborted')
    if not 's3_db_bucket' in env:
        abort('Please configure an env.s3_db_bucket for this project.')

    # record the environments
    dst_env = _get_env(dst)

    # If there's no host defined, assume localhost and run tasks locally.
    if not dst_env.hosts:
        run_function = local
    else:
        run_function = run

    # get S3 key path for src
    opts_string += ' --region=us-west-2'
    dump = _get_s3_key(opts_string)

    dst_env.db_host = _mysql_db_host(dst_env)

    # Drop the previous tables in dst in case it has tables not in src.
    drop_tables_sql = """mysql -h %(db_host)s -u%(db_user)s -p%(db_pw)s -BNe "show tables" %(db_db)s \
        | tr '\n' ',' | sed -e 's/,$//' \
        | awk '{print "SET FOREIGN_KEY_CHECKS = 0;DROP TABLE IF EXISTS " $1 ";SET FOREIGN_KEY_CHECKS = 1;"}' \
        | mysql -h %(db_host)s -u%(db_user)s -p%(db_pw)s %(db_db)s"""

    run_function(drop_tables_sql % {"db_host": dst_env.db_host,
        "db_user": dst_env.db_user, "db_pw": dst_env.db_pw,
        "db_db": dst_env.db_db})

    import_sql = 'mysql -h %s -u%s -p%s -D%s' % (dst_env.db_host,
            dst_env.db_user, dst_env.db_pw, dst_env.db_db)
    run_function('aws s3 cp %s %s - | gunzip -c | %s' % (opts_string, dump, import_sql))
    print('+ Database synced from %s to %s' % (src, dst))

def _mysql_db_host(local_env):
    """
    Figures out the correct host to use for database moving calls.
    """
    db_host = getattr(local_env, 'db_host', 'localhost')
    hosts = getattr(local_env, 'hosts', [])
    if db_host == 'localhost' and len(hosts):
        db_host = hosts[0]
    return db_host

def _get_env(env_name):
    """
    Returns an env object for env_name without overwriting the global env.
    """
    global env
    previous = deepcopy(env)
    execute(env_name)
    local_env = deepcopy(env)

    # Delete any attributes that were added.
    for key, value in local_env.iteritems():
        if not key in previous:
            del env[key]

    # Put the original values back on env.
    for key, value in previous.iteritems():
        env[key] = value

    # If loading local environment, unset hosts, since it can cause problems.
    if env_name == 'local':
        local_env['hosts'] = []

    return local_env

def _get_s3_key(opts_string):
    """
    Returns S3 sql dump key path for environment

    If dump doesn't exist, a new one will be created.
    """
    global env
    src = env.tasks[0]
    src_directory = env.s3_namespace + '.' + src + '/'

    # @todo: warn user if can't connection
    connection = S3Connection()
    bucket = connection.lookup(env.s3_db_bucket)
    if bucket is None:
        abort('Bucket %s not found in S3' % env.s3_db_bucket)

    # Find a sql dump within the past 5 days.
    keys = bucket.list(src_directory, "/")
    date_format = "%Y-%m-%dT%H:%M:%S"
    date_limit = datetime.today() - timedelta(days=5)
    valid_dump = False
    if list(keys):
        for key in _filter_s3_files(keys):
            if datetime.strptime(key.last_modified[:19], date_format) >= date_limit:
                valid_dump = 's3://' + env.s3_db_bucket + '/' + key.name
                break;

    # If a recent dump hasn't been found, create a new one.
    if not valid_dump:
        dump_sql = 'mysqldump -h %s -u%s -p%s %s' % (env.db_host,
                env.db_user, env.db_pw, env.db_db)
        valid_dump = 's3://%s/%s%s.sql.gz' % (env.s3_db_bucket,
                src_directory, datetime.today().strftime('%Y%m%d'))
        tmp_file = '/tmp/%s-%s.%d.sql.gz' % (env.s3_namespace, src, int(time()))
        run('%s | gzip -c > %s && aws s3 cp %s %s %s && rm %s' % (dump_sql,
                tmp_file, opts_string, tmp_file, valid_dump, tmp_file))

    return valid_dump

def _filter_s3_files(keys):
    """
    Filters out directories from a list of S3 keys
    """
    return (key for key in keys if not key.name.endswith('/'))
