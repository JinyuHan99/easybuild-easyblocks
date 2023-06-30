##
# Copyright 2018-2023 Free University of Brussels (VUB)
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
EasyBuild support for MXNet, implemented as an easyblock

@author: Ward Poelmans (Free University of Brussels)
"""
import glob
import os
import shutil
from distutils.version import LooseVersion

from easybuild.easyblocks.generic.makecp import MakeCp
from easybuild.easyblocks.generic.pythonpackage import PythonPackage
from easybuild.easyblocks.generic.rpackage import RPackage
from easybuild.framework.easyconfig import CUSTOM
from easybuild.tools.build_log import EasyBuildError
from easybuild.tools.filetools import change_dir, mkdir, remove_dir, symlink, write_file
from easybuild.tools.modules import get_software_root, get_software_version
from easybuild.tools.run import run_cmd
from easybuild.tools.systemtools import get_shared_lib_ext

# the namespace file for the R extension
R_NAMESPACE = """# Export all names
exportPattern(".")

# Import all packages listed as Imports or Depends
import(
methods,
Rcpp,
DiagrammeR,
data.table,
jsonlite,
magrittr,
stringr
)
"""


class EB_MXNet(MakeCp):
    """Easyblock to build and install MXNet"""

    @staticmethod
    def extra_options():
        """Change default values of options"""
        extra_vars = {
            'install_r_ext': [False, "Enable installation of R extensions", CUSTOM],
        }
        extra = MakeCp.extra_options(extra_vars)
        # files_to_copy is not mandatory here
        extra['files_to_copy'][2] = CUSTOM

        return extra

    def __init__(self, *args, **kwargs):
        """Initialize custom class variables."""
        super(EB_MXNet, self).__init__(*args, **kwargs)

        self.mxnet_src_dir = None
        self.py_ext = PythonPackage(self, {'name': self.name, 'version': self.version})
        self.py_ext.module_generator = self.module_generator
        self.r_ext = RPackage(self, {'name': self.name, 'version': self.version})
        self.r_ext.module_generator = self.module_generator

    def extract_step(self):
        """
        Prepare a combined MXNet source tree. Move all submodules
        to their right place.
        """
        # Extract everything into separate directories.
        super(EB_MXNet, self).extract_step()

        mxnet_dirs = glob.glob(os.path.join(self.builddir, '*mxnet-*'))
        if len(mxnet_dirs) == 1:
            self.mxnet_src_dir = mxnet_dirs[0]
            self.log.debug("MXNet dir is: %s", self.mxnet_src_dir)
        else:
            raise EasyBuildError("Failed to find/isolate MXNet source directory: %s", mxnet_dirs)

        for srcdir in [d for d in os.listdir(self.builddir) if d != os.path.basename(self.mxnet_src_dir)]:
            submodule, _, _ = srcdir.rpartition('-')

            # if newdir starts with 'oneDNN-', we rename it to mkldnn:
            if submodule == 'oneDNN':
                submodule = 'mkldnn'
                # rename the file to 'mkldnn':
                old_srcdir = srcdir
                srcdir = srcdir.replace('oneDNN', 'mkldnn')
                os.rename(os.path.join(self.builddir, old_srcdir), os.path.join(self.builddir, srcdir))

            olddir = os.path.join(self.builddir, srcdir)
            newdir = os.path.join(self.mxnet_src_dir, '3rdparty', submodule)

            # first remove empty existing directory
            remove_dir(newdir)

            try:
                shutil.move(olddir, newdir)
            except IOError as err:
                raise EasyBuildError("Failed to move %s to %s: %s", olddir, newdir, err)

    def prepare_step(self, *args, **kwargs):
        """Prepare for building and installing MXNet."""
        super(EB_MXNet, self).prepare_step(*args, **kwargs)
        self.py_ext.prepare_python()

    def configure_step(self):
        """Patch 'config.mk' file to use EB stuff"""
        for (var, env_var) in [('CC', 'CC'), ('CXX', 'CXX'), ('ADD_CFLAGS', 'CFLAGS'), ('ADD_LDFLAGS', 'LDFLAGS')]:
            self.cfg.update('buildopts', '%s="%s"' % (var, os.getenv(env_var)))

        toolchain_blas = self.toolchain.definition().get('BLAS', None)[0]
        if toolchain_blas == 'imkl':
            blas = "mkl"
            imkl_version = get_software_version('imkl')
            if LooseVersion(imkl_version) >= LooseVersion('17'):
                self.cfg.update('buildopts', 'USE_MKL2017=1')
            self.cfg.update('buildopts', 'MKLML_ROOT="%s"' % os.getenv("MKLROOT"))
        elif toolchain_blas in ['ACML', 'ATLAS']:
            blas = "atlas"
        elif toolchain_blas == 'OpenBLAS':
            blas = "openblas"
        elif toolchain_blas == 'FlexiBLAS':
            blas = "flexiblas"
        elif toolchain_blas is None:
            raise EasyBuildError("No BLAS library found in the toolchain")

        self.cfg.update('buildopts', 'USE_BLAS="%s"' % blas)

        if get_software_root('NNPACK'):
            self.cfg.update('buildopts', 'USE_NNPACK=1')

        super(EB_MXNet, self).configure_step()

    def install_step(self):
        """Specify list of files to copy"""
        self.cfg['files_to_copy'] = ['bin', 'include', 'lib']
        super(EB_MXNet, self).install_step()

    def extensions_step(self):
        """Build & Install both Python and R extension"""
        # we start with the python bindings
        self.py_ext.src = os.path.join(self.mxnet_src_dir, "python")
        change_dir(self.py_ext.src)

        self.py_ext.prerun()
        self.py_ext.run(unpack_src=False)
        self.py_ext.postrun()
        
        if self.cfg['install_r_ext']:
            # This is off by default, because it's been working in the old version of MXNet and now it's not.
            # Also, from the website of MXNet, Python bindings seem to be the preferred ones so we'll focus on that.
            self.install_r_ext()
        else:
            self.log.debug("Skipping R extension installation")

    def install_r_ext(self):
        # next up, the R bindings
        self.r_ext.src = os.path.join(self.mxnet_src_dir, "R-package")
        change_dir(self.r_ext.src)
        mkdir("inst")
        symlink(os.path.join(self.installdir, "lib"), os.path.join("inst", "libs"))
        symlink(os.path.join(self.installdir, "include"), os.path.join("inst", "include"))

        # MXNet doesn't provide a list of its R dependencies by default
        write_file("NAMESPACE", R_NAMESPACE)
        change_dir(self.mxnet_src_dir)
        self.r_ext.prerun()  # tried commenting this out
        # MXNet is just weird. To install the R extension, we have to:
        # - First install the extension like it is
        # - Let R export the extension again. By doing this, all the dependencies get
        #   correctly filled and some mappings are done
        # - Reinstal the exported version
        self.r_ext.run()
        run_cmd("R_LIBS=%s Rscript -e \"require(mxnet); mxnet:::mxnet.export(\\\"R-package\\\")\"" % self.installdir)
        change_dir(self.r_ext.src)
        self.r_ext.run()
        self.r_ext.postrun()

    def sanity_check_step(self):
        """Check for main library files for MXNet"""
        custom_paths = {
            'files': ['lib/libmxnet.a', 'lib/libmxnet.%s' % get_shared_lib_ext()],
            'dirs': [],
        }
        super(EB_MXNet, self).sanity_check_step(custom_paths=custom_paths)

        # for the extension we are doing the loading of the fake module ourself
        try:
            fake_mod_data = self.load_fake_module()
        except EasyBuildError as err:
            raise EasyBuildError("Loading fake module failed: %s", err)

        if not self.py_ext.sanity_check_step():
            raise EasyBuildError("The sanity check for the Python bindings failed")

        self.r_ext.options['modulename'] = self.name.lower()
        if not self.r_ext.sanity_check_step():
            raise EasyBuildError("The sanity check for the R bindings failed")

        self.clean_up_fake_module(fake_mod_data)

    def make_module_extra(self, *args, **kwargs):
        """Custom variables for MXNet module."""
        txt = super(EB_MXNet, self).make_module_extra(*args, **kwargs)

        for path in self.py_ext.all_pylibdirs:
            fullpath = os.path.join(self.installdir, path)
            # only extend $PYTHONPATH with existing, non-empty directories
            if os.path.exists(fullpath) and os.listdir(fullpath):
                txt += self.module_generator.prepend_paths('PYTHONPATH', path)

        txt += self.module_generator.prepend_paths("R_LIBS", [''])  # prepend R_LIBS with install path

        return txt
