from alembic.config import Config
from alembic.environment import EnvironmentContext
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from difflib import unified_diff
from pkg_resources import resource_filename
from re import split
from sqlalchemy import engine_from_config
from subprocess import call, Popen, PIPE


from restbase.testing import get_distribution
project_name = get_distribution().project_name


# we use our own db for this test, since it will be created and dropped
db_name = '%s_migration_test' + project_name
settings = {
    'sqlalchemy.url': 'postgresql:///' + db_name,
    'testing': True,
}


def createdb():
    call('createdb %s -E utf8 -T template0' % db_name, shell=True)


def drobdb():
    call('dropdb %s' % db_name, shell=True)


def dumpdb():
    p = Popen('pg_dump %s' % db_name, shell=True, stdout=PIPE, stderr=PIPE)
    out, err = p.communicate()
    assert p.returncode == 0, err
    # we parse the output and change it a little bit for better diffing
    out = out.splitlines()
    start = None
    for index, line in enumerate(out):
        # we only change CREATE TABLE statements
        if line.startswith('CREATE TABLE'):
            start = index
        if start is not None:
            if line.strip().endswith(');'):
                # we sort the columns
                out[start + 1:index] = sorted(out[start + 1:index])
                start = None
            else:
                # and remove trailing commas
                out[index] = line.rstrip().rstrip(',')
        else:
            # for COPY statements, we have to sort the column names as well
            if line.startswith('COPY'):
                parts = split('[()]', line)
                columns = sorted(x.strip() for x in parts[1].split(','))
                out[index] = '%s(%s)%s' % (parts[0], ' ,'.join(columns), parts[2])
    # we add newlines for diffing
    return ['%s\n' % x for x in out]


def test_db_metadata_differences(package):
    # first we drop anything there might be
    drobdb()
    # then we create a clean DB from the metadata
    createdb()
    from restbase import models
    metadata = models.metadata
    engine = engine_from_config(settings)
    metadata.bind = engine
    metadata.create_all(engine)
    # and store the results
    create_all_result = dumpdb()
    engine.dispose()
    # now we do it again, but this time using migrations
    drobdb()
    createdb()
    config = Config()
    config.set_main_option('script_location',
        resource_filename(project_name, '../alembic'))
    script = ScriptDirectory.from_config(config)
    connection = engine.connect()
    environment = EnvironmentContext(config, script,
        starting_rev='base', destination_rev='head')
    context = MigrationContext.configure(connection)

    def upgrade(rev, context):
        return script._upgrade_revs('head', rev)

    context._migrations_fn = upgrade
    environment._migration_context = context
    with environment.begin_transaction():
        environment.run_migrations()
    # we drop alembic_version to avoid it showing up in the diff
    engine.execute('DROP TABLE alembic_version;')
    # we store these results
    alembic_result = dumpdb()
    del context
    del environment
    connection.close()
    del connection
    engine.dispose()
    # now we check whether there are differences and output them if there are
    diff = unified_diff(create_all_result, alembic_result)
    assert create_all_result == alembic_result, \
        'Migration output differs:\n' + ''.join(diff)
