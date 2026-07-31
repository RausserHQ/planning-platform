# Migration and cleanup

The pre-existing OpenSWE/LangGraph lab and the private Linear/OMX executor are
not part of this platform. They remain isolated and frozen as rollback points
until all three pilots pass.

After acceptance:

1. export planning artifacts with ongoing operational value;
2. disable legacy webhook routes and schedules;
3. revoke legacy mutation credentials;
4. remove obsolete workloads through their owning GitOps repositories;
5. verify that only Windmill receives OpenProject/GitHub planning events;
6. retain the final pre-cutover commits and backups for the documented
   rollback window.

Do not migrate historical noise and do not delete old data before acceptance.
