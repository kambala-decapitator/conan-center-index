from conan import ConanFile
from conan.tools.scm import Version
from conan.tools.files import get, chdir, replace_in_file, copy, rmdir, export_conandata_patches, apply_conandata_patches
from conan.tools.microsoft import is_msvc, MSBuildToolchain, VCVars, unix_path
from conan.tools.layout import basic_layout
from conan.tools.gnu import Autotools, AutotoolsToolchain
from conan.tools.apple import XCRun, to_apple_arch
from conan.tools.build import cross_building
from conan.tools.env import VirtualBuildEnv
from conan.errors import ConanInvalidConfiguration
import os


required_conan_version = ">=1.53.0"


class LuajitConan(ConanFile):
    name = "luajit"
    license = "MIT"
    url = "https://github.com/conan-io/conan-center-index"
    homepage = "http://luajit.org"
    description = "LuaJIT is a Just-In-Time Compiler (JIT) for the Lua programming language."
    topics = ("lua", "jit")
    provides = "lua"
    settings = "os", "arch", "compiler", "build_type"
    options = {"shared": [True, False], "fPIC": [True, False]}
    default_options = {"shared": False, "fPIC": True}

    def export_sources(self):
        export_conandata_patches(self)

    def config_options(self):
        if self.settings.os == "Windows":
            del self.options.fPIC

    def configure(self):
        if self.options.shared:
            self.options.rm_safe("fPIC")
        self.settings.rm_safe("compiler.libcxx")
        self.settings.rm_safe("compiler.cppstd")

    def layout(self):
        basic_layout(self, src_folder="src")

    @property
    def _is_host_32bit(self):
        return self.settings.arch in ["armv7", "x86"]

    def validate(self):
        if self.settings.os == "Macos" and self.settings.arch == "armv8" and cross_building(self):
            raise ConanInvalidConfiguration(f"{self.ref} can not be cross-built to Mac M1. Please, try any version >=2.1")
        elif Version(self.version) <= "2.1.0-beta1" and self.settings.os == "Macos" and self.settings.arch == "armv8":
            raise ConanInvalidConfiguration(f"{self.ref} is not supported by Mac M1. Please, try any version >=2.1")
        elif self._is_host_32bit and self.settings_build.os == "Macos":
            # well, technically it should work on macOS <= 10.14
            raise ConanInvalidConfiguration(f"{self.ref} cannot be cross-built to a 32-bit platform on macOS, see https://github.com/LuaJIT/LuaJIT/issues/664")

    def source(self):
        filename = f"LuaJIT-{self.version}.tar.gz"
        get(self, **self.conan_data["sources"][self.version], destination=self.source_folder, filename=filename, strip_root=True)

    def generate(self):
        if is_msvc(self):
            tc = MSBuildToolchain(self)
            tc.generate()
            tc = VCVars(self)
            tc.generate()
        else:
            tc = AutotoolsToolchain(self)
            env = tc.environment()
            if self.settings.os == "iOS" or self.settings.os == "Android":
                env.define("CFLAGS", "")
                env.define("LDFLAGS", "")
            tc.generate(env)

    def _patch_sources(self):
        if not is_msvc(self):
            buildmode = 'shared' if self.options.shared else 'static'
            makefile = os.path.join(self.source_folder, 'src', 'Makefile')
            replace_in_file(self, makefile,
                                  'BUILDMODE= mixed',
                                  'BUILDMODE= %s' % buildmode)
            replace_in_file(self, makefile,
                                  'TARGET_DYLIBPATH= $(TARGET_LIBPATH)/$(TARGET_DYLIBNAME)',
                                  'TARGET_DYLIBPATH= $(TARGET_DYLIBNAME)')
            # adjust mixed mode defaults to build either .so or .a, but not both
            if not self.options.shared:
                replace_in_file(self, makefile,
                                      'TARGET_T= $(LUAJIT_T) $(LUAJIT_SO)',
                                      'TARGET_T= $(LUAJIT_T) $(LUAJIT_A)')
                replace_in_file(self, makefile,
                                      'TARGET_DEP= $(LIB_VMDEF) $(LUAJIT_SO)',
                                      'TARGET_DEP= $(LIB_VMDEF) $(LUAJIT_A)')
            else:
                replace_in_file(self, makefile,
                                      'TARGET_O= $(LUAJIT_A)',
                                      'TARGET_O= $(LUAJIT_SO)')

    def _apple_deployment_target(self, default=None):
        return self.settings.get_safe("os.version", default=default)

    @property
    def _make_arguments(self):
        args = [f"PREFIX={unix_path(self, self.package_folder)}"]
        if "clang" in str(self.settings.compiler):
            args.append("DEFAULT_CC=clang")

        if self.settings.os == "Macos" and self._apple_deployment_target():
            args.append(f"MACOSX_DEPLOYMENT_TARGET={self._apple_deployment_target()}")
        elif self.settings.os == "iOS":
            xcrun = XCRun(self)
            target_flag = f"{to_apple_arch(self)}-apple-ios{self._apple_deployment_target(default='')}"
            args.extend([
                f"CROSS={os.path.dirname(xcrun.cxx)}/",
                f"""TARGET_FLAGS='-isysroot "{xcrun.sdk_path}" -target {target_flag}'""",
                "TARGET_SYS=iOS",
            ])
        elif self.settings.os == "Android":
            buildenv_vars = VirtualBuildEnv(self).vars()
            compiler_path = buildenv_vars.get("CC")
            triplet_prefix = f"{buildenv_vars.get('CHOST')}-"
            args.extend([
                f"CROSS={os.path.join(buildenv_vars.get('NDK_ROOT'), 'bin', triplet_prefix)}",
                f"DYNAMIC_CC='{compiler_path} -fPIC'",
                f"STATIC_CC={compiler_path}",
                f"TARGET_AR='{buildenv_vars.get('AR')} rcus'",
                f"TARGET_LD={compiler_path}",
                f"TARGET_STRIP={buildenv_vars.get('STRIP')}",
            ])
            if self.settings_build.os != "Linux":
                args.append("TARGET_SYS=Linux")
            if self.settings_build.os == "Macos":
                # must look for headers in macOS SDK, having NDK clang in PATH breaks this default behavior
                xcrun_build = XCRun(self, sdk='macosx')
                isysroot_flag = f"""'-isysroot "{xcrun_build.sdk_path}"'"""
                args.extend([
                    f"HOST_CFLAGS={isysroot_flag}",
                    f"HOST_LDFLAGS={isysroot_flag}",
                ])
        return args

    @property
    def _luajit_include_folder(self):
        luaversion = Version(self.version)
        if luaversion.major == "2":
            return f"luajit-{luaversion.major}.{luaversion.minor}"
        return "luajit-2.1"

    def build(self):
        apply_conandata_patches(self)
        self._patch_sources()
        if is_msvc(self):
            with chdir(self, os.path.join(self.source_folder, "src")):
                variant = '' if self.options.shared else 'static'
                self.run(f"msvcbuild.bat {variant}", env="conanbuild")
        else:
            with chdir(self, self.source_folder):
                autotools = Autotools(self)
                autotools.make(args=self._make_arguments)

    def package(self):
        copy(self, "COPYRIGHT", dst=os.path.join(self.package_folder, "licenses"), src=self.source_folder)
        src_folder = os.path.join(self.source_folder, "src")
        include_folder = os.path.join(self.package_folder, "include", self._luajit_include_folder)
        if is_msvc(self):
            copy(self, "lua.h", src=src_folder, dst=include_folder)
            copy(self, "lualib.h", src=src_folder, dst=include_folder)
            copy(self, "lauxlib.h", src=src_folder, dst=include_folder)
            copy(self, "luaconf.h", src=src_folder, dst=include_folder)
            copy(self, "lua.hpp", src=src_folder, dst=include_folder)
            copy(self, "luajit.h", src=src_folder, dst=include_folder)
            copy(self, "lua51.lib", src=src_folder, dst=os.path.join(self.package_folder, "lib"))
            copy(self, "lua51.dll", src=src_folder, dst=os.path.join(self.package_folder, "bin"))
        else:
            with chdir(self, self.source_folder):
                autotools = Autotools(self)
                autotools.install(args=self._make_arguments + ["DESTDIR="])
            rmdir(self, os.path.join(self.package_folder, "lib", "pkgconfig"))
            rmdir(self, os.path.join(self.package_folder, "share"))

    def package_info(self):
        self.cpp_info.libs = ["lua51" if is_msvc(self) else "luajit-5.1"]
        self.cpp_info.set_property("pkg_config_name", "luajit")
        self.cpp_info.includedirs = [os.path.join("include", self._luajit_include_folder)]
        if self.settings.os in ["Linux", "FreeBSD"]:
            self.cpp_info.system_libs.extend(["m", "dl"])
