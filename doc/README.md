* For installation, see /README.md
* For exercise configuration, see /courses/README.md

# Gitmanager Filesystem Walkthrough

* `/doc`: Description of the system and material for system administrators.

* `/gitmanager`: Django project settings, urls and wsgi accessor.

* `/builder`: Course building and exercise configuring.

* `/templates`: Base templates for default Git Manager pages.

* `/static`: Static files for default Git Manager pages.

* `/access`: Django application presenting courses.

	* `templates`: View templates.

	* `types`: Implementations for different exercise view types.

	* `management`: Commandline interface for testing configured exercises.

* `/util`: Utility modules for HTTP, shell, git, filesystem access etc.

* `/courses`: Default COURSES_PATH for holding course exercise configuration and material.

* `/course_store`: Default STORE_PATH for temporarily storing built course exercise configuration and material.

* `/scripts`: Python modules for different types of builders.
