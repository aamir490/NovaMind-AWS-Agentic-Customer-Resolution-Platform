# Workflow scope

`ci.yml` runs offline backend tests plus frontend Node tests and a production build
for pull requests targeting `main`. It has read-only repository permissions and
does not configure AWS credentials, publishing, or deployment.
