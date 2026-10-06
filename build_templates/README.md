# Dockerfile templates for the build pipeline.
#
# Layout per application type:
#
#   build_templates/<application_type>/<version>/Dockerfile.j2
#   build_templates/<application_type>/<version>/template.yaml
#
# `template.yaml` declares the variables the template needs, their validators
# and the image's default port -- the pipeline validates against it before it
# renders anything (Phase 5).
#
# The version actually used per type is pinned in ACTIVE_TEMPLATE_VERSIONS, so
# a template can be revised without changing a line of pipeline code.
#
# These are NOT Django templates: Django's template loader never looks here.
# `templates/` in the project root stays reserved for Django itself.
#
# Empty in Phase 0; filled in Phase 5.
