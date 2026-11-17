-- Launchplane's own database and its login, made once. The login has no password: only the system user launchplane on
-- this machine may use it, through the local socket. The encoding is named because Debian makes its cluster in SQL_ASCII.
select 'create role launchplane login' where not exists (select from pg_roles where rolname = 'launchplane') \gexec
select 'create database launchplane owner launchplane encoding ''UTF8'' locale ''C.UTF-8'' template template0' where not exists (select from pg_database where datname = 'launchplane') \gexec
revoke connect on database launchplane from public;
