#!/usr/bin/python -tt
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
# (c) 2005 seth vidal skvidal at phy.duke.edu


# helps partially track a repo. Let's you download a package + all of its
# deps from any set of repos.
# use: so you can keep current on any given pkg + its deps from another
#      repo w/o enabling that repo in your yum configuration by default
#      also for making partial mirrors that traverse dependencies.

from __future__ import absolute_import, division, print_function
from builtins import (ascii, bytes, chr, dict, filter, hex, input,
                      int, map, next, oct, open, pow, range, round,
                      str, super, zip)
import os
import sys
import shutil
from optparse import OptionParser
from urlparse import urljoin
import logging

import fnmatch
import yum
import yum.Errors
import rpmUtils

from yum.misc import getCacheDir
from yum.constants import *
from yum.packages import parsePackages
from yum.packageSack import ListPackageSack
from yum.i18n import to_unicode


class groupQuery:
    def __init__(self, group, grouppkgs="required"):
        self.grouppkgs = grouppkgs
        self.id = group.groupid
        self.name = group.name
        self.group = group

    def doQuery(self, method, *args, **kw):
        if hasattr(self, "fmt_%s" % method):
            return "\n".join(getattr(self, "fmt_%s" % method)(*args, **kw))
        else:
            raise queryError("Invalid group query: %s" % method)

    # XXX temporary hack to make --group -a query work
    def fmt_queryformat(self, **kw):
        return self.fmt_nevra()

    def fmt_nevra(self, **kw):
        return ["%s - %s" % (self.id, self.name)]

    def fmt_list(self, **kw):
        pkgs = []
        for t in self.grouppkgs.split(','):
            if t == "mandatory":
                pkgs.extend(self.group.mandatory_packages)
            elif t == "default":
                pkgs.extend(self.group.default_packages)
            elif t == "optional":
                pkgs.extend(self.group.optional_packages)
            elif t == "all":
                pkgs.extend(self.group.packages)
            else:
                raise queryError("Unknown group package type %s" % t)

        return pkgs

    def fmt_requires(self, **kw):
        return self.group.mandatory_packages

    def fmt_info(self, **kw):
        return ["%s:\n\n%s\n" % (self.name, self.group.description)]


class queryError(Exception):
    def __init__(self, value=None):
        Exception.__init__(self)
        self.value = value
    def __str__(self):
        return "%s" %(self.value,)

    def __unicode__(self):
        return '%s' % to_unicode(self.value)


class RepoTrack(yum.YumBase):
    def __init__(self, opts):
        yum.YumBase.__init__(self)
        self.logger = logging.getLogger("yum.verbose.repotrack")
        self.opts = opts

    def findDeps(self, po):
        """Return the dependencies for a given package, as well
           possible solutions for those dependencies.

           Returns the deps as a dict  of:
            dict[reqs] = [list of satisfying pkgs]"""


        reqs = po.returnPrco('requires')
        reqs.sort()
        pkgresults = {}

        for req in reqs:
            (r,f,v) = req
            if r.startswith('rpmlib('):
                continue

            pkgresults[req] = list(self.whatProvides(r, f, v))

        return pkgresults

    def returnGroups(self):
        grps = []
        for group in self.comps.get_groups():
            grp = groupQuery(group, grouppkgs = "all")
            grps.append(grp)
        return grps

    def matchGroups(self, items):
        grps = []
        for grp in self.returnGroups():
            for expr in items:
                if grp.name == expr or fnmatch.fnmatch("%s" % grp.name, expr):
                    grps.append(grp)
                elif grp.id == expr or fnmatch.fnmatch("%s" % grp.id, expr):
                    grps.append(grp)

        return grps


def more_to_check(unprocessed_pkgs):
    for pkg in unprocessed_pkgs.keys():
        if unprocessed_pkgs[pkg] is not None:
            return True

    return False


def parseArgs():
    usage = """
    Repotrack: keep current on any given pkg and its deps. It will download the package(s) you
               want to track and all of their dependencies

    %s [options] package1 [package2] [package..]    """ % sys.argv[0]

    parser = OptionParser(usage=usage)
    parser.add_option("-c", "--config", default='/etc/yum.conf',
                      help='config file to use (defaults to /etc/yum.conf)')
    parser.add_option("-a", "--arch", default=None,
                      help='check as if running the specified arch (default: current arch)')
    parser.add_option("-r", "--repoid", default=[], action='append',
                      help="specify repo ids to query, can be specified multiple times (default is all enabled)")
    parser.add_option("-t", "--tempcache", default=False, action="store_true",
                      help="Use a temp dir for storing/accessing yum-cache")
    parser.add_option("-p", "--download_path", dest='destdir',
                      default=os.getcwd(), help="Path to download packages to")
    parser.add_option("-u", "--urls", default=False, action="store_true",
                      help="Just list urls of what would be downloaded, don't download")
    parser.add_option("-n", "--newest", default=True, action="store_false",
                      help="Toggle downloading only the newest packages(defaults to newest-only)")
    parser.add_option("-q", "--quiet", default=False, action="store_true",
                      help="Output as little as possible")
    parser.add_option("-x", "--exclude", default=None,
                      help='exclude package or partial string')
    parser.add_option("-g", "--group", default=[], action='append',
                      help="groups to query in addition to supplied packages, can be specified multiple times")

    (opts, args) = parser.parse_args()
    return (opts, args)


def main():

    (opts, user_pkg_list) = parseArgs()
    print("type:{} Value:{}".format(type(opts), opts))
    exit(0)
    if len(user_pkg_list) == 0 and not opts.group:
        print >> sys.stderr, "Error: no packages specified to parse"
        sys.exit(1)

    if not os.path.exists(opts.destdir) and not opts.urls:
        try:
            os.makedirs(opts.destdir)
        except OSError, e:
            print >> sys.stderr, "Error: Cannot create destination dir %s" % opts.destdir
            sys.exit(1)

    if not os.access(opts.destdir, os.W_OK) and not opts.urls:
        print >> sys.stderr, "Error: Cannot write to  destination dir %s" % opts.destdir
        sys.exit(1)


    my = RepoTrack(opts=opts)
    my.doConfigSetup(fn=opts.config, init_plugins=False) # init yum, without plugins

    if opts.group:
        my.doGroupSetup()

    pkgs = my.matchGroups(opts.group)
    for pkg in pkgs:
        tmp = pkg.fmt_list()
        user_pkg_list += tmp

    if opts.arch:
        archlist = []
        archlist.extend(rpmUtils.arch.getArchList(opts.arch))
    else:
        archlist = rpmUtils.arch.getArchList()

    # do the happy tmpdir thing if we're not root
    if os.geteuid() != 0 or opts.tempcache:
        cachedir = getCacheDir()
        if cachedir is None:
            print >> sys.stderr, "Error: Could not make cachedir, exiting"
            sys.exit(50)

        my.repos.setCacheDir(cachedir)

    if len(opts.repoid) > 0:
        myrepos = []

        # find the ones we want
        for glob in opts.repoid:
            myrepos.extend(my.repos.findRepos(glob))

        # disable them all
        for repo in my.repos.repos.values():
            repo.disable()

        # enable the ones we like
        for repo in myrepos:
            repo.enable()
            my._getSacks(archlist=archlist, thisrepo=repo.id)

    my.doRepoSetup()
    my._getSacks(archlist=archlist)

    unprocessed_pkgs = {}
    final_pkgs = {}
    pkg_list = []

    avail = my.pkgSack.returnPackages()
    for item in user_pkg_list:
        exactmatch, matched, unmatched = parsePackages(avail, [item])
        pkg_list.extend(exactmatch)
        pkg_list.extend(matched)
        if opts.newest:
            this_sack = ListPackageSack()
            this_sack.addList(pkg_list)
            pkg_list = this_sack.returnNewestByNameArch()
            del this_sack

    if len(pkg_list) == 0:
        print >> sys.stderr, "Nothing found to download matching packages specified"
        sys.exit(1)

    for po in pkg_list:
        unprocessed_pkgs[po.pkgtup] = po


    while more_to_check(unprocessed_pkgs):
        for pkgtup in unprocessed_pkgs.keys():
            if unprocessed_pkgs[pkgtup] is None:
                continue

            po = unprocessed_pkgs[pkgtup]
            final_pkgs[po.pkgtup] = po

            deps_dict = my.findDeps(po)
            unprocessed_pkgs[po.pkgtup] = None
            for req in deps_dict.keys():
                pkg_list = deps_dict[req]
                if opts.newest:
                    this_sack = ListPackageSack()
                    this_sack.addList(pkg_list)
                    pkg_list = this_sack.returnNewestByNameArch()
                    del this_sack

                for res in pkg_list:
                    if res is not None and res.pkgtup not in unprocessed_pkgs:
                        unprocessed_pkgs[res.pkgtup] = res

    if opts.exclude:
        for key, package in final_pkgs.items():
            if opts.exclude in str(package):
                del final_pkgs[key]

    download_list = final_pkgs.values()
    print download_list
    if opts.newest:
        this_sack = ListPackageSack()
        this_sack.addList(download_list)
        download_list = this_sack.returnNewestByNameArch()

    download_list.sort(key=lambda pkg: pkg.name)
    for pkg in download_list:
        repo = my.repos.getRepo(pkg.repoid)
        remote = pkg.returnSimple('relativepath')
        local = os.path.basename(remote)
        local = os.path.join(opts.destdir, local)
        if (os.path.exists(local) and
                    os.path.getsize(local) == int(pkg.returnSimple('packagesize'))):

            if not opts.quiet:
                my.logger.info("%s already exists and appears to be complete" % local)
            continue

        if opts.urls:
            url = urljoin(repo.urls[0], remote)
            print '%s' % url
            continue

        # Disable cache otherwise things won't download
        repo.cache = 0
        if not opts.quiet:
            my.logger.info('Downloading %s' % os.path.basename(remote))
            """
            This is the anasible location that would set the state to changed.
            """
        pkg.localpath = local  # Hack: to set the localpath to what we want.
        path = repo.getPackage(pkg)

        if not os.path.exists(local) or not os.path.samefile(path, local):
            shutil.copy2(path, local)


if __name__ == "__main__":
    main()
