from collections import defaultdict, Counter
from contextlib import suppress
from json import load
from os import environ, makedirs
from os.path import dirname, exists, join
from shutil import rmtree
from traceback import TracebackException

from .exceptions import MetadataPersistentKeyError
from .metadata import DoNotRunAgain, metadata_to_json
from .node import _flatten_group_hierarchy
from .utils import list_starts_with, randomize_order, NO_DEFAULT
from .utils.dicts import extra_paths_in_dict
from .utils.ui import io, QUIT_EVENT
from .utils.metastack import Metastack
from .utils.text import bold, mark_for_translation as _, red


MAX_METADATA_ITERATIONS = int(environ.get("BW_MAX_METADATA_ITERATIONS", "1000"))


class PathSet:
    """
    Collects metadata paths and stores only the highest levels ones.

    >>> s = PathSet()
    >>> s.add(("foo", "bar"))
    >>> s.add(("foo",))
    >>> s
    {"foo"}
    """

    def __init__(self):
        self._paths = set()

    def __iter__(self):
        for path in self._paths:
            yield path

    def __len__(self):
        return len(self._paths)

    def __repr__(self):
        return "<PathSet: {}>".format(repr(self._paths))

    def add(self, new_path):
        if self.covers(new_path):
            return False
        for existing_path in self._paths.copy():
            if list_starts_with(existing_path, new_path):
                self._paths.remove(existing_path)
        self._paths.add(new_path)
        return True

    def covers(self, candidate_path):
        for existing_path in self._paths:
            if list_starts_with(candidate_path, existing_path):
                return True
        return False


def reactors_for_paths(available_reactors, required_paths):
    """
    Returns only those available_reactors that might affect the
    required_paths.
    """
    for reactor in available_reactors:
        provides = getattr(reactor, '_provides', tuple())
        if provides:
            for path in provides:
                if required_paths.covers(path):
                    yield reactor
                    break
        else:
            yield reactor


class NodeMetadataProxy:
    def __init__(self, metagen, node):
        self._metagen = metagen
        self._node = node
        self._metastack = Metastack()
        self._metastack_came_from_cache = None
        self._completed_reactors = set()
        self._requested_paths = PathSet()
        self._satisfied = False  # has this node completed all required reactors?
        self.__relevant_reactors_cache = None

    def __getitem__(self, key):
        return self.get((key,))

    def __iter__(self):
        for key, value in self.get(tuple()).items():
            yield key, value

    @property
    def _relevant_reactors(self):
        """
        All reactors that might provide some of the requested paths.
        """
        if self.__relevant_reactors_cache is None:
            self.__relevant_reactors_cache = set(reactors_for_paths(
                self._node.metadata_reactors,
                self._requested_paths,
            ))
        return self.__relevant_reactors_cache

    @property
    def _pending_reactors(self):
        """
        All reactors that might provide some of the requested paths and
        have not yet been run to completion.
        """
        for reactor in self._relevant_reactors:
            if reactor not in self._completed_reactors:
                yield reactor

    def __disk_cache_node_filename(self):
        return join(self._metagen._disk_cache_hash_dir, self._node.name)

    def __read_disk_cache(self):
        if not environ.get("BW_METADATA_CACHE_DIR"):
            raise FileNotFoundError

        with open(self.__disk_cache_node_filename(), 'rb') as f:
            return load(f)

    def _write_disk_cache(self):
        node_file = self.__disk_cache_node_filename()
        if not exists(node_file):
            makedirs(dirname(node_file), mode=0o770, exist_ok=True)
            with open(node_file, 'w') as f:
                f.write(metadata_to_json(self._metastack._as_dict()))

    def __ensure_uncached_metastack(self):
        """
        Cached stacks are flat and useless for .blame and .stack
        """
        if self._metastack_came_from_cache:
            self._metastack = Metastack()
            self._satisfied = False
        with self._metagen._node_metadata_lock:
            self._metagen._build_node_metadata(self._node.name)

    @property
    def blame(self):
        if self._metagen._in_a_reactor:
            raise RuntimeError("cannot call node.metadata.blame from a reactor")
        else:
            self.__ensure_uncached_metastack()
            return self._metastack._as_blame()

    @property
    def stack(self):
        if self._metagen._in_a_reactor:
            raise RuntimeError("cannot call node.metadata.stack from a reactor")
        else:
            self.__ensure_uncached_metastack()
            return self._metastack

    def get(self, path, default=NO_DEFAULT):
        if not isinstance(path, (tuple, list)):
            path = tuple(path.split("/"))
        if self._requested_paths.add(path):
            self._satisfied = False
            self.__relevant_reactors_cache = None

        if self._metastack_came_from_cache is None:
            try:
                metadata = self.__read_disk_cache()
            except FileNotFoundError:
                pass
            else:
                self._metastack = Metastack()
                self._metastack._set_layer(0, "flattened", metadata)
                self._metastack_came_from_cache = True
                self._satisfied = True

        if self._metagen._in_a_reactor:
            self._metagen._partial_metadata_accessed_for.add(self._node.name)
        else:
            with self._metagen._node_metadata_lock:
                self._metagen._build_node_metadata(self._node.name)

        try:
            return self._metastack.get(path)
        except KeyError:
            if default != NO_DEFAULT:
                return default
            else:
                raise


class _StartOver(Exception):
    """
    Raised when metadata processing needs to start from the top.
    """
    pass


class MetadataGenerator:
    # are we currently executing a reactor?
    _in_a_reactor = False
    # should reactor return values be checked against their declared keys?
    _verify_reactor_provides = False

    def __reset(self):
        # reactors that raised DoNotRunAgain
        self.__do_not_run_again = set()
        # reactors that raised KeyErrors (and which ones)
        self.__keyerrors = {}
        # mapping each node to all nodes that depend on it
        self.__node_deps = defaultdict(set)
        # how often __run_reactors was called for a node
        self.__node_iterations = defaultdict(int)
        # A node is 'stable' when all its relevant reactors return unchanged
        # metadata, except for those reactors that look at other nodes.
        # This dict maps nodes to True/False indicating stable status.
        self.__node_stable = {}
        # nodes we encountered as a dependency through partial_metadata,
        # but haven't run yet
        self.__nodes_that_never_ran = set()
        # nodes whose dependencies changed and that have to rerun their
        # reactors depending on those nodes
        self.__triggered_nodes = set()
        # nodes we already did initial processing on
        self.__nodes_that_ran_at_least_once = set()
        # how often we called reactors
        self.__reactors_run = 0
        # how often each reactor changed
        self.__reactor_changes = defaultdict(int)
        # tracks which reactors on a node have looked at other nodes
        # through partial_metadata
        self.__reactors_with_deps = defaultdict(set)

    def _metadata_proxy_for_node(self, node_name):
        if node_name not in self._node_metadata_proxies:
            self._node_metadata_proxies[node_name] = NodeMetadataProxy(self, self.get_node(node_name))
        return self._node_metadata_proxies[node_name]

    @property
    def _disk_cache_hash_dir(self):
        if not environ.get("BW_METADATA_CACHE_DIR"):
            return None
        return join(
            environ.get("BW_METADATA_CACHE_DIR"),
            self.hash_for_files_changing_metadata,
        )

    def clear_metadata_cache(self):
        if self._disk_cache_hash_dir:
            io.debug(f"removing {self._disk_cache_hash_dir}")
            rmtree(self._disk_cache_hash_dir)

    def __run_new_nodes(self):
        try:
            node_name = self.__nodes_that_never_ran.pop()
        except KeyError:
            pass
        else:
            self.__nodes_that_ran_at_least_once.add(node_name)
            self.__initial_run_for_node(node_name)
            raise _StartOver

    def __run_triggered_nodes(self):
        try:
            node_name = self.__triggered_nodes.pop()
        except KeyError:
            pass
        else:
            io.debug(f"triggered metadata run for {node_name}")
            self.__run_reactors(
                self.get_node(node_name),
                with_deps=True,
                without_deps=False,
            )
            raise _StartOver

    def __run_unstable_nodes(self):
        encountered_unstable_node = False
        for node, stable in self.__node_stable.items():
            if stable:
                continue

            io.debug(f"begin metadata stabilization test for {node.name}")
            self.__run_reactors(node, with_deps=False, without_deps=True)
            if self.__node_stable[node]:
                io.debug(f"metadata stabilized for {node.name}")
            else:
                io.debug(f"metadata remains unstable for {node.name}")
                encountered_unstable_node = True
            if self.__nodes_that_never_ran:
                # we have found a new dependency, process it immediately
                # going wide early should be more efficient
                raise _StartOver
        if encountered_unstable_node:
            # start over until everything is stable
            io.debug("found an unstable node (without_deps=True)")
            raise _StartOver

    def __run_nodes_with_deps(self):
        encountered_unstable_node = False
        for node in randomize_order(self.__node_stable.keys()):
            io.debug(f"begin final stabilization test for {node.name}")
            self.__run_reactors(node, with_deps=True, without_deps=False)
            if not self.__node_stable[node]:
                io.debug(f"{node.name} still unstable")
                encountered_unstable_node = True
            if self.__nodes_that_never_ran:
                # we have found a new dependency, process it immediately
                # going wide early should be more efficient
                raise _StartOver
        if encountered_unstable_node:
            # start over until everything is stable
            io.debug("found an unstable node (with_deps=True)")
            raise _StartOver

    def _build_node_metadata(self, initial_node_name):
        if self._node_metadata_proxies[initial_node_name]._satisfied:
            return

        self.__reset()
        self.__nodes_that_never_ran.add(initial_node_name)

        while not QUIT_EVENT.is_set():
            jobmsg = _("{b} ({n} nodes, {r} reactors, {e} runs)").format(
                b=bold(_("running metadata reactors")),
                n=len(self.__nodes_that_never_ran) + len(self.__nodes_that_ran_at_least_once),
                r=len(self.__reactor_changes),
                e=self.__reactors_run,
            )
            try:
                with io.job(jobmsg):
                    # Control flow here is a bit iffy. The functions in this block often raise
                    # _StartOver in order to aggressively process new nodes first etc.
                    # Each method represents a distinct stage of metadata processing that checks
                    # for nodes in certain states as described below.

                    # This checks for newly discovered nodes that haven't seen any processing at
                    # all so far. It is important that we run them as early as possible, so their
                    # static metadata becomes available to other nodes and we recursively discover
                    # additional nodes as quickly as possible.
                    self.__run_new_nodes()
                    # At this point, we have run all relevant nodes at least once.

                    # Nodes become "triggered" when they previously looked something up from a
                    # different node and that second node changed. In this method, we try to figure
                    # out if the change on the node we depend on actually has any effect on the
                    # depending node.
                    self.__run_triggered_nodes()

                    # In this stage, we run all unstable nodes to the point where everything is
                    # stable again, except for those reactors that depend on other nodes.
                    self.__run_unstable_nodes()

                    # The final step is to make sure nothing changes when we run reactors with
                    # dependencies on other nodes. If anything changes, we need to start over so
                    # local-only reactors on a node can react to changes caused by reactors looking
                    # at other nodes.
                    self.__run_nodes_with_deps()

                    # If we get here, we're done! All that's left to do is blacklist completed
                    # reactors so they don't get run again if additional metadata is requested.
                    for node in self.__node_stable:
                        proxy = self._node_metadata_proxies[node.name]
                        proxy._completed_reactors.update(
                            proxy._relevant_reactors
                        )
                        proxy._satisfied = True
                        proxy._metastack_came_from_cache = False
                        if (
                            environ.get("BW_METADATA_CACHE_DIR") and
                            proxy._requested_paths.covers(tuple())  # full metadata
                        ):
                            proxy._write_disk_cache()
                    break

            except _StartOver:
                continue

        if self.__keyerrors and not QUIT_EVENT.is_set():
            msg = _(
                "These metadata reactors raised a KeyError "
                "even after all other reactors were done:"
            )
            for source, exc in sorted(self.__keyerrors.items()):
                node_name, reactor = source
                msg += f"\n\n  {node_name} {reactor}\n\n"
                for line in TracebackException.from_exception(exc).format():
                    msg += "    " + line
            raise MetadataPersistentKeyError(msg)

        io.debug("metadata generation for selected nodes finished")

    def __initial_run_for_node(self, node_name):
        io.debug(f"initial metadata run for {node_name}")
        node = self.get_node(node_name)

        # randomize order to increase chance of exposing clashing defaults
        for defaults_name, defaults in randomize_order(node.metadata_defaults):
            node.metadata._metastack._set_layer(
                2,
                defaults_name,
                defaults,
            )
        node.metadata._metastack._cache_partition(2)

        group_order = _flatten_group_hierarchy(node.groups)
        for group_name in group_order:
            node.metadata._metastack._set_layer(
                0,
                "group:{}".format(group_name),
                self.get_group(group_name)._attributes.get('metadata', {}),
            )

        node.metadata._metastack._set_layer(
            0,
            "node:{}".format(node_name),
            node._attributes.get('metadata', {}),
        )
        node.metadata._metastack._cache_partition(0)

        # run all reactors once to get started
        self.__run_reactors(node, with_deps=True, without_deps=True)

    def __check_iteration_count(self, node_name):
        self.__node_iterations[node_name] += 1
        if self.__node_iterations[node_name] > MAX_METADATA_ITERATIONS:
            top_changers = Counter(self.__reactor_changes).most_common(25)
            msg = _(
                "MAX_METADATA_ITERATIONS({m}) exceeded for {node}, "
                "likely an infinite loop between flip-flopping metadata reactors.\n"
                "These are the reactors that changed most often:\n\n"
            ).format(m=MAX_METADATA_ITERATIONS, node=node_name)
            for reactor, count in top_changers:
                msg += f"  {count}\t{reactor[0]}\t{reactor[1]}\n"
            raise RuntimeError(msg)

    def __run_reactors(self, node, with_deps=True, without_deps=True):
        self.__check_iteration_count(node.name)
        any_reactor_changed = False

        for depsonly in (True, False):
            if depsonly and not with_deps:
                # skip reactors with deps
                continue
            if not depsonly and not without_deps:
                # skip reactors without deps
                continue
            # TODO ideally, we should run the least-run reactors first
            for reactor_name, reactor in randomize_order(
                self._node_metadata_proxies[node.name]._pending_reactors
            ):
                if (
                    (depsonly and reactor_name not in self.__reactors_with_deps[node.name]) or
                    (not depsonly and reactor_name in self.__reactors_with_deps[node.name])
                ):
                    # this if makes sure we run reactors with deps first
                    continue
                reactor_changed, deps = self.__run_reactor(node, reactor_name, reactor)
                io.debug(f"{node.name}:{reactor_name} changed={reactor_changed} deps={deps}")
                if reactor_changed:
                    any_reactor_changed = True
                if deps:
                    # record that this reactor has dependencies
                    self.__reactors_with_deps[node.name].add(reactor_name)
                    # we could also remove this marker if we end up without
                    # deps again in future iterations, but that is too
                    # unlikely and the housekeeping cost too great
                for required_node_name in deps:
                    if required_node_name not in self.__nodes_that_ran_at_least_once:
                        # we found a node that we didn't need until now
                        self.__nodes_that_never_ran.add(required_node_name)
                    # this is so we know the current node needs to be run
                    # again if the required node changes
                    self.__node_deps[required_node_name].add(node.name)

        if any_reactor_changed:
            # something changed on this node, mark all dependent nodes as unstable
            for required_node_name in self.__node_deps[node.name]:
                io.debug(f"{node.name} triggering metadata rerun on {required_node_name}")
                self.__triggered_nodes.add(required_node_name)

        if with_deps and any_reactor_changed:
            self.__node_stable[node] = False
        elif without_deps:
            self.__node_stable[node] = not any_reactor_changed

    def __run_reactor(self, node, reactor_name, reactor):
        if (node.name, reactor_name) in self.__do_not_run_again:
            return False, set()
        self._partial_metadata_accessed_for = set()
        self.__reactors_run += 1
        # make sure the reactor doesn't react to its own output
        old_metadata = node.metadata._metastack._pop_layer(1, reactor_name)
        self._in_a_reactor = True
        try:
            new_metadata = reactor(node.metadata)
        except KeyError as exc:
            self.__keyerrors[(node.name, reactor_name)] = exc
            return False, self._partial_metadata_accessed_for
        except DoNotRunAgain:
            self.__do_not_run_again.add((node.name, reactor_name))
            # clear any previously stored exception
            with suppress(KeyError):
                del self.__keyerrors[(node.name, reactor_name)]
            return False, set()
        except Exception as exc:
            io.stderr(_(
                "{x} Exception while executing metadata reactor "
                "{metaproc} for node {node}:"
            ).format(
                x=red("!!!"),
                metaproc=reactor_name,
                node=node.name,
            ))
            raise exc
        finally:
            self._in_a_reactor = False

        # reactor terminated normally, clear any previously stored exception
        with suppress(KeyError):
            del self.__keyerrors[(node.name, reactor_name)]

        if self._verify_reactor_provides and getattr(reactor, '_provides', None):
            extra_paths = extra_paths_in_dict(new_metadata, reactor._provides)
            if extra_paths:
                raise ValueError(_(
                    "{reactor_name} on {node_name} returned the following key paths, "
                    "but didn't declare them with @metadata_reactor.provides():\n"
                    "{paths}"
                ).format(
                    node_name=node.name,
                    reactor_name=reactor_name,
                    paths="\n".join(["/".join(path) for path in sorted(extra_paths)]),
                ))

        try:
            node.metadata._metastack._set_layer(
                1,
                reactor_name,
                new_metadata,
            )
        except TypeError as exc:
            # TODO catch validation errors better
            io.stderr(_(
                "{x} Exception after executing metadata reactor "
                "{metaproc} for node {node}:"
            ).format(
                x=red("!!!"),
                metaproc=reactor_name,
                node=node.name,
            ))
            raise exc

        changed = old_metadata != new_metadata
        if changed:
            self.__reactor_changes[(node.name, reactor_name)] += 1

        return changed, self._partial_metadata_accessed_for
