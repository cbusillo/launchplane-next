-- Launchplane's own database and its login, made once. The login has no password: only the system user launchplane on
-- this machine may use it, through the local socket.
select 'create role launchplane login' where not exists (select from pg_roles where rolname = 'launchplane') \gexec
select 'create database launchplane owner launchplane' where not exists (select from pg_database where datname = 'launchplane') \gexec
revoke connect on database launchplane from public;
