from __future__ import with_statement
from functools import wraps, partial
import os, os.path
import itertools

from fabric.api import *
from fabric.contrib.files import append, exists
from fabric.contrib.files import upload_template as orig_upload_template

ROLES = ['nginx', 'django', 'database', 'smtp']
DJANGO_PORT = 81
BRANCH = 'master'
PROJECT_NAME = 'fixjam'
PROJECT_DIR = '/project/%s' % PROJECT_NAME
DB_PASS = '290jsg2390j32t93j932093' # Should not contain quotes; coupled w/settings.py
GIT_REPOSITORY = 'git@github.com:reverie/fixjam.git'

# TODO: better way to support roles/roledefs -- patch fabric??
# XXX: upload_template does not preserve file permissions, http://code.fabfile.org/issues/show/117

env.virtualenv = 'default'
SERVER_GROUP = 'app'

#
# Hax
#

def ssudo(*args, **kwargs):
    """Task version of sudo :|"""
    return sudo(*args,**kwargs)

def rrun(*args, **kwargs):
    """Task version of run :|"""
    return run(*args,**kwargs)

def upload_template(src, dest, *args, **kwargs):
    """
    My wrapped version that sets +r.
    """
    orig_upload_template(src, dest, *args, **kwargs)
    sudo('chmod +r %s' % dest)

def boxed_task(name):
    """So you can use e.g. Pip.install_requirements as a task."""
    box, task = name.split('.', 1)
    box = globals()[box]
    task = getattr(box, task)
    task()

#
# Stage management
#

def stage_dev():
    env.NUM_UPDATERS = 1
    env.stage = {
            'hostname': 'dev.fixjam.com'
        }
    env.my_roledefs = dict(
        zip(
            ROLES, 
            itertools.repeat([(env.stage['hostname'], '127.0.0.1')])
        )
    )
    env.hosts = [env.stage['hostname']]

def stage_staging():
    env.NUM_UPDATERS = 1
    env.user = 'ubuntu'
    env.stage = {
            'hostname': 'staging.fixjam.com' # AWS i-d44ea790 
        }
    env.my_roledefs = dict(
        zip(
            ROLES, 
            itertools.repeat([(env.stage['hostname'], '127.0.0.1')])
        )
    )
    env.hosts = [env.stage['hostname']]

def stage_production():
    # Here, we don't assign hosts, because we need full-manual control of hosts. 
    # Fabric does not support the kind of operations I want to do.
    # Don't even use the real roledefs, because Fabric might do something with them.
    env.NUM_UPDATERS = 4
    env.user = 'andrew'
    env.stage = {
            'hostname': 'www.fixjam.com'
        }
    env.my_roledefs = dict(
        zip(
            ROLES, 
            itertools.repeat([(env.stage['hostname'], '127.0.0.1')])
        )
    )
    env.hosts = [env.stage['hostname']]
    assert set(env.my_roledefs.keys()) == set(ROLES)
    # These assumptions are used e.g. in nginx config:
    assert len(env.my_roledefs['django']) == 1
    assert len(env.my_roledefs['database']) == 1
    assert len(env.my_roledefs['smtp']) == 1

#
# Host selection
#

def all_hosts():
    """Call this after the stage to set hosts to all hosts in that stage."""
    hosts = set()
    for role in ROLES:
        for external, internal in env.my_roledefs[role]:
            hosts.add(external)
    env.hosts = list(hosts) # Fabric tries to add

def hosts_in_role(role):
    env.hosts = Roledefs.get_external_hostnames(role)

class Roledefs(object):
    @staticmethod
    def get_external_hostnames(role):
        return [x[0] for x in env.my_roledefs[role]]

    @staticmethod
    def get_internal_ips(role):
        return [x[1] for x in env.my_roledefs[role]]

    @staticmethod
    def get_internal_ip(role):
        host_pairs = env.my_roledefs[role]
        assert len(host_pairs) == 1
        return host_pairs[0][1]

    @staticmethod
    def get_roles():
        """List of roles that we are currently processing."""
        roles = set()
        for role in ROLES:
            if env.host in Roledefs.get_external_hostnames(role):
                roles.add(role)
        assert roles
        return roles

    @staticmethod
    def role_matches(*args):
        """Check if the current host matches any of the roles in *args."""
        roles = Roledefs.get_roles()
        for role in args:
            assert role in ROLES, "Unknown role %s passed to role_matches" % (role)
            if role in roles:
                return True
        return False


#
# Tasks
#

class Apt(object):
    @staticmethod
    def install(*pkgs):
        sudo('apt-get install -y %s' % ' '.join(pkgs))

    @staticmethod
    def upgrade():
        sudo('apt-get update -y')
        sudo('apt-get upgrade -y')

class Pip(object):
    @staticmethod
    def install_virtualenv():
        # Only sudo fxn here
        sudo('pip install virtualenv')

    @staticmethod
    def install(*pkgs):
        require('virtualenv')
        for pkg in pkgs:
            run('pip install -E %s -U %s' % (get_env_dir(), pkg))

    @staticmethod
    def install_requirements():
        REMOTE_FILENAME = './tmp_requirements.txt'
        require('virtualenv')
        put('./server/requirements.txt', REMOTE_FILENAME)
        run('pip install -E %s -r %s' % (get_env_dir(), REMOTE_FILENAME))
        run('rm %s' % REMOTE_FILENAME)

def get_env_dir():
    return '/envs/%s' % env.virtualenv

def setup_permissions(dirname):
    sudo('chown -R %s:%s %s' % (env.user, SERVER_GROUP, dirname))
    sudo('chmod -R g+w %s' % dirname)

def adduser(username):
    # Idempotent (non-failing) version of adduser
    base_cmd = 'useradd --user-group %s' % username
    sudo(base_cmd + ' || [[ $? == 9 ]]') # 9 is failure code for already exists
    # alt: getent passwd username || useradd, also thanks to \amethyst

def bootstrap_everything():
    print "bootstrap everything"
    install_common()
    install_nginx()
    install_database()
    install_django()
    install_smtp()
    configure_nginx()
    configure_django()
    configure_database()
    restart_database() # Must be done before deploy so that syncdb works
    dumb_deploy()
    restart_database()
    restart_django() # Must be done before nginx so that port 80 is free
    restart_nginx()
    print '*'*20
    print 'Now either restart or "sudo /usr/bin/svscanboot &" (fixme)'
    print '*'*20

def bootstrap_database():
    assert Roledefs.role_matches('database')
    install_common()
    install_database()
    configure_database()
    restart_database()

def bootstrap_nginx():
    assert Roledefs.role_matches('nginx')
    install_common()
    install_nginx()
    configure_nginx()
    deploy()
    restart_nginx()

def bootstrap_django():
    assert Roledefs.role_matches('django')
    install_common()
    install_django()
    configure_django()
    deploy()
    restart_django()

def bootstrap_smtp():
    assert Roledefs.role_matches('smtp')
    install_common()
    install_smtp()

def install_common():
    print "install common"
    # Need to do this first or else Grub will prompt for some bullshit.
    # TODO: test if it works
    put('./server/grub_preseed.cfg', 'grub_preseed.cfg')
    sudo('debconf-set-selections grub_preseed.cfg')
    Apt.upgrade()
    sudo('echo LANG=\\"en_US.UTF-8\\" > /etc/default/locale')
    locale_env = [
        'LANGUAGE="en_US.utf8"',
        'LANG="en_US.utf8"'
    ]
    append(locale_env, '/etc/environment', use_sudo=True)
    Apt.install('python-setuptools', 'python-pycurl', 'vim', 'screen', 'language-pack-en', 'git-core',
            'subversion', 'cron', 'curl', 'man', 'build-essential', 'python-dev', 'libpq-dev',
            'python-psycopg2', 'libcurl4-gnutls-dev', 'debconf-utils', 'ntp'
            )
    sudo('easy_install -U setuptools')
    sudo('easy_install pip')
    adduser(SERVER_GROUP)
    for dirname in ['releases', 'packages', 'bin', 'log']:
        sudo('mkdir -p %s' % os.path.join(PROJECT_DIR, dirname))
    setup_permissions('/project')
    install_private_key()

def install_private_key():
    run('mkdir -p ~/.ssh')
    put('./server/id_rsa', '~/.ssh/id_rsa')
    run('chmod 600 ~/.ssh/id_rsa')
    # So we can git clone from git@github.com w/o manual setup
    put('./server/known_hosts', '~/.ssh/known_hosts')

def install_nginx():
    assert Roledefs.role_matches('nginx')
    Apt.install('nginx')
    assert exists('/etc/nginx/sites-enabled') # Right package install format?
    if exists('/etc/nginx/sites-enabled/default'):
        sudo('rm /etc/nginx/sites-enabled/default')
    install_processor()

def install_processor():
    """
    Stuff to compile javascript (and other file processing later?). 
    Separate function from install_nginx so it's easier to update 
    server-side code.
    """
    assert Roledefs.role_matches('nginx')
    put('./server/processor/compiler.jar', os.path.join(PROJECT_DIR, 'bin', 'compiler.jar'))
    put('./server/processor/processor', os.path.join(PROJECT_DIR, 'bin', 'processor'))

def install_django():
    assert Roledefs.role_matches('django')
    Pip.install_virtualenv()
    env_dir = get_env_dir()
    if not exists(env_dir):
        # TODO: may not install virtualenv if it failed earlier.
        # better test than exists?
        sudo('mkdir -p %s' % env_dir)
        sudo('virtualenv --no-site-packages %s' % env_dir)
    setup_permissions(env_dir)
    Pip.install_requirements()
    Apt.install('apache2', 'postgresql-client', 'libapache2-mod-wsgi')
    if exists('/etc/apache2/sites-enabled/000-default'):
        sudo('rm /etc/apache2/sites-enabled/000-default')
    sudo('usermod -G %s -a www-data' % SERVER_GROUP)

def install_smtp():
    # TODO: *render* this instead for hostname:
    put('./server/postfix_preseed.cfg', 'postfix_preseed.cfg')
    sudo('debconf-set-selections postfix_preseed.cfg')
    Apt.install('postfix')
    run('rm postfix_preseed.cfg')

def install_database():
    assert Roledefs.role_matches('database')
    # NB: If EC2, make sure device is mounted. See AWS_NOTES.
    #raw_input('If this is the EC2 server, make sure EBS drive is mounted before continuing. Press enter to continue.')
    Apt.install('postgresql')
    sudo('mkdir -p /datastore')
    # Drop default cluster if it exists
    # TODO: use pg_lsclusters instead of [ -d ..]
    sudo('if [ -d /etc/postgresql/8.4/main ]; then pg_dropcluster --stop 8.4 main; fi')
    # Note: PROJECT_NAME is coupled with config files' destination
    # Dir /datastore/pgdb is coupled with generated postgresql.conf, so update our customized one if you change that.
    sudo('if [ ! -d /datastore/pgdb ]; then pg_createcluster -p 5432 --encoding=UTF8 --locale=en_US.UTF8 -d /datastore/pgdb --start 8.4 %s; fi' % PROJECT_NAME)
    # Ensure it's started in case this wasn't the first install:
    restart_database()

def sudo_put(local_file, remote_file, new_owner='root'):
    # TODO: make sure remote_file isn't the containing dir for the new file
    put(local_file, 'tmp')
    sudo('mv tmp %s' % remote_file)
    sudo('chown %s:%s %s' % (new_owner, new_owner, remote_file))

def configure_nginx():
    assert Roledefs.role_matches('nginx')
    sudo_put('./server/nginx/nginx.conf', '/etc/nginx/nginx.conf')
    upload_template('./server/nginx/%s' % PROJECT_NAME, '/etc/nginx/sites-available/%s' % PROJECT_NAME, use_sudo=True, use_jinja=True, context={
        'hostname': env.stage['hostname'],
        'django_host': Roledefs.get_internal_ip('django'),
        'DJANGO_PORT': DJANGO_PORT,
    })
    if not exists('/etc/nginx/sites-enabled/%s' % PROJECT_NAME):
        sudo('ln -s /etc/nginx/sites-available/%s /etc/nginx/sites-enabled/%s' % (PROJECT_NAME, PROJECT_NAME))

def configure_django():
    assert Roledefs.role_matches('django')
    put('./server/django/wsgi.py', os.path.join(PROJECT_DIR, 'wsgi.py'))
    upload_template('./server/django/vhost', '/etc/apache2/sites-available/%s' % PROJECT_NAME, use_sudo=True, use_jinja=True, context={
        'DJANGO_PORT': DJANGO_PORT,
    })
    upload_template('./server/django/ports.conf', '/etc/apache2/ports.conf', use_sudo=True, use_jinja=True, context={
        'DJANGO_PORT': DJANGO_PORT,
    })
    upload_template('./server/django/stagesettings.py', os.path.join(PROJECT_DIR, 'stagesettings.py'), use_sudo=True, 
        use_jinja=True, context={
        'database_host': Roledefs.get_internal_ip('database'),
    })
    if not exists('/etc/apache2/sites-enabled/%s' % PROJECT_NAME):
        sudo('ln -s /etc/apache2/sites-available/%s /etc/apache2/sites-enabled/%s' % (PROJECT_NAME, PROJECT_NAME))

def run_with_safe_error(cmd, safe_error, use_sudo=False, user=None):
    # Todo: use _run_command in 1.0
    if user:
        assert use_sudo
    if use_sudo:
        runner = partial(sudo, user=user)
    else:
        runner = run
    with settings(warn_only=True):
        result = runner(cmd)
    if not result.failed:
        return result
    if result == safe_error: # Will probably end up using 'in' instead of '==', but wait and see
        return result
    # FAIL: this can't work right now b/c we don't have access to stderr. Wait for Fabric 1.0
    return result # Remove this.
    abort("Command had unexpected error:\n" + 
            "  Command: %s\n" % cmd + 
            "  Expected error: %s\n" % safe_error + 
            "  Actual error: %s" % result
            )

def configure_database():
    assert Roledefs.role_matches('database')
    config_dir = '/etc/postgresql/8.4/%s' % PROJECT_NAME
    sudo('mkdir -p %s' % config_dir)
    for filename in ['environment', 'pg_ctl.conf', 'pg_hba.conf', 'pg_ident.conf', 'postgresql.conf', 'start.conf']:
        sudo_put(os.path.join('./server/database', filename), os.path.join(config_dir, filename), new_owner='postgres')
    run_with_safe_error("createdb %s" % PROJECT_NAME, 'some dumb error', use_sudo=True, user='postgres')
    run_with_safe_error("""psql -c "create user %s with createdb encrypted password '%s'" """ % (PROJECT_NAME, DB_PASS), "some dumb error", use_sudo=True, user='postgres')
    sudo("""psql -c "grant all privileges on database %s to %s" """ % (PROJECT_NAME, PROJECT_NAME), user='postgres')

def make_symlink_atomically(new_target, symlink_location, sudo=False):
    # From http://blog.moertel.com/articles/2005/08/22/how-to-change-symlinks-atomically
    runner = sudo if sudo else run
    params = {
            'new_target': new_target,
            'symlink_location': symlink_location,
            'tempname': 'current_tmp',
            }
    cmd = "ln -s %(new_target)s %(tempname)s && mv -Tf %(tempname)s %(symlink_location)s" % params
    runner(cmd)

def deploy_related(f):
    @wraps(f)
    def new_task(*args, **kwargs):
        if not Roledefs.role_matches('nginx', 'django'):
            print "Skipping %s on this server." % f.__name__
            return
        return f(*args, **kwargs)
    return new_task

class Deploy(object):

    @staticmethod
    @deploy_related
    def get_current_commit():
        return local('git rev-parse --verify %s' % BRANCH).strip()

    @staticmethod
    @deploy_related
    def switch_symlink(name):
        assert name
        new_target = os.path.join(PROJECT_DIR, 'releases', name)
        symlink_location = os.path.join(PROJECT_DIR, 'current')
        make_symlink_atomically(new_target, symlink_location)

    @staticmethod
    @deploy_related
    def get_release_dir(name):
        assert name
        return os.path.join(PROJECT_DIR, 'releases', name)
 
    @staticmethod
    @deploy_related
    def upload_new_release():
        name = Deploy.get_current_commit()
        release_dir = Deploy.get_release_dir(name)
        if exists(release_dir):
            assert release_dir.startswith(os.path.join(PROJECT_DIR, 'releases'))
            run('rm -rf %s' % release_dir)
        run('git clone %s %s' % (GIT_REPOSITORY, release_dir))
        with cd(release_dir):
            run('git reset --hard %s' % name)
        return name

    @staticmethod
    @deploy_related
    def prep_release(name):
        """Prepares all the files in the release dir."""
        assert name
        release_dir = Deploy.get_release_dir(name)
        django_dir = os.path.join(release_dir, PROJECT_NAME)
        # Don't need any processing yet -- commented out:
        #if Roledefs.role_matches('nginx'):
        #    if int(os.environ.get('FBC_DEBUG', 0)):
        #        raw_input("Warning: you are deploying a debug release. This might break things. Press any key to continue.")
        #    else:
        #        # Processing of static files
        #        run(os.path.join(PROJECT_DIR, 'bin', 'processor') + ' ' + release_dir)
        if Roledefs.role_matches('django'):
            print 'Doing django deploy component'
            with cd(django_dir):
                run('ln -nfs %s .' % os.path.join(PROJECT_DIR, 'stagesettings.py'))
                run('ln -nfs %s .' % os.path.join(PROJECT_DIR, 'localsettings.py'))
                run('source /envs/default/bin/activate && python manage.py syncdb --noinput')
                run('source /envs/default/bin/activate && python manage.py migrate')
                run('source /envs/default/bin/activate && python manage.py loaddata initial_data')

    @staticmethod
    @deploy_related
    def cleanup_release(name):
        pkg_filename = "%s.tar.gz" % name
        if os.path.exists(pkg_filename):
            local('rm %s' % pkg_filename)


@deploy_related
def list_releases():
    with cd(os.path.join(PROJECT_DIR, 'releases')):
        run('ls -ltc | grep -v total | cut -d " " --fields=6,7,8 | head -n 10')
        run('ls -l %s | cut -d " " -f "10"' % os.path.join(PROJECT_DIR, 'current'))

prep_release = Deploy.prep_release

def deploy_prep_new_release():
    local('git push')
    release_name = Deploy.upload_new_release()
    Deploy.prep_release(release_name)
    print '*'*20
    print "Prepped new release", release_name
    print 'You probably want to deploy_activate_release:%s' % release_name
    print '*'*20

def deploy_activate_release(release_name):
    assert release_name
    Deploy.switch_symlink(release_name)
    restart_after_deploy()
    Deploy.cleanup_release(release_name)

def deploy():
    release_name = Deploy.upload_new_release()
    Deploy.prep_release(release_name)
    Deploy.switch_symlink(release_name)
    Deploy.cleanup_release(release_name)

def dumb_deploy():
    local('git push')
    deploy()
    restart_after_deploy()

def restart_after_deploy():
    if Roledefs.role_matches('django'):
        restart_django()

def reload_nginx():
    sudo('initctl reload nginx')

def reload_django():
    sudo('apache2ctl graceful')

def reload_database():
    sudo('/etc/init.d/postgresql-8.4 reload')

def restart_nginx():
    sudo('/etc/init.d/nginx restart')

def restart_django():
    sudo('apache2ctl graceful || apache2ctl start')

def restart_database():
    sudo('/etc/init.d/postgresql-8.4 restart || /etc/init.d/postgresql-8.4 start')

def down_for_maintenance():
    assert Roledefs.role_matches('nginx')
    with cd(os.path.join(PROJECT_DIR, 'current', 'static')):
        run('cp index.html index.html.bak')
        run('cp down.html index.html')

def comingsoon():
    assert Roledefs.role_matches('nginx')
    with cd(os.path.join(PROJECT_DIR, 'current', 'static')):
        run('cp index.html index.html.bak')
        run('cp comingsoon.html index.html')
