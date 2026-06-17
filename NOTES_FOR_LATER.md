# Notes for Later

- **Investigate removal of path tracker from Store ABC**: Path tracking could be a scheduler-only concern, maintaining something resembling a path tracker for scheduling decisions. `LocalDBStore` should still handle `registrationTime` updating, but that's separate from path tracking.
