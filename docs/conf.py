from datetime import datetime

# -- Project information -----------------------------------------------------

project = u'CinCan Command'

year = datetime.now().year
copyright = f'{year} CinCan Project'
# The full version, including alpha/beta/rc tags
release = 'https://gitlab.com/cincan/cincan-command'

master_doc = "index"

html_theme_options = {
    "description": "A convenient way to run dockerized command-line tools.",
    # "fixed_sidebar": True,
}

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = ['recommonmark']
source_suffix = {
    '.rst': 'restructuredtext',
    '.txt': 'markdown',
    '.md': 'markdown',
}

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', ]


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'alabaster'
html_sidebars = {
    '**': [
        'about.html',
        'navigation.html',
        'relations.html',
        'searchbox.html',
    ]
}
# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']


# Page source url
html_show_sourcelink = False
# Powered by
show_powered_by = False
html_show_sphinx = False
# Copyright
html_show_copyright = True