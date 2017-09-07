import collections
import sublime
import sublime_plugin
import subprocess
import threading
import re

from concurrent import futures
from threading import RLock

import xml.etree.ElementTree as etree

# sublime-rtags settings
settings = None
# path to rc utility
RC_PATH = ''
rc_timeout = 0.5
auto_complete = True
# fixits and errors - this is still in super early pre alpha state
fixits = False


def run_rc(switches, input=None, quote=True, *args):
    p = subprocess.Popen([RC_PATH] + switches + list(args),
                         stderr=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         stdin=subprocess.PIPE)
    if quote:
        print(' '.join(p.args))
    return p.communicate(input=input, timeout=rc_timeout)


def get_view_text(view):
    return bytes(view.substr(sublime.Region(0, view.size())), "utf-8")


def supported_file_type(view):
    if settings == None:
        return False
    file_types = settings.get('file_types', ["source.c", "source.c++"])
    return view.scope_name(view.sel()[0].a).split()[0] in file_types


class NavigationHelper(object):
    NAVIGATION_REQUESTED = 1
    NAVIGATION_DONE = 2

    def __init__(self):
        # navigation indicator, possible values are:
        # - NAVIGATION_REQUESTED
        # - NAVIGATION_DONE
        self.flag = NavigationHelper.NAVIGATION_DONE
        # rc utility switches to use for callback
        self.switches = []
        # file contents that has been passed to reindexer last time
        self.data = ''
        # history of navigations
        # elements are tuples (filename, line, col)
        self.history = collections.deque()


class ProgressIndicator():

    def __init__(self):
        self.addend = 1
        self.size = 8
        self.last_view = None
        self.window = None
        self.busy = False
        self.status_key = settings.get('status_key', 'rtags_status_indicator')

    def set_busy(self, busy):
        if self.busy == busy:
            return
        self.busy = busy
        sublime.set_timeout(lambda: self.run(1), 0)

    def run(self, i):
        if self.window is None:
            self.window = sublime.active_window()
        active_view = self.window.active_view()

        if self.last_view is not None and active_view != self.last_view:
            self.last_view.erase_status(self.status_key)
            self.last_view = None

        # check if we are currently indexing
        out, err = run_rc(['--is-indexing', '--silent-query'], None, False)
        if out.decode().strip() != "1":
            self.busy = False
            active_view.erase_status(self.status_key)
            return

        before = i % self.size
        after = (self.size - 1) - before

        active_view.set_status(self.status_key, 'RTags [%s=%s]' % (' ' * before, ' ' * after))
        if self.last_view is None:
            self.last_view = active_view

        if not after:
            self.addend = -1
        if not before:
            self.addend = 1
        i += self.addend

        sublime.set_timeout(lambda: self.run(i), 100)


class RConnectionThread(threading.Thread):

    def notify(self):
        sublime.active_window().active_view().run_command('rtags_location',
                                                          {'switches': navigation_helper.switches})

    def run(self):
        self.p = subprocess.Popen([RC_PATH, '-m', '--silent-query'],
                                  stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        # `rc -m` will feed stdout with xml like this:
        #
        # <?xml version="1.0" encoding="utf-8"?>
        #  <checkstyle>
        #   <file name="/home/ramp/tmp/pthread_simple.c">
        #    <error line="54" column="5" severity="warning" message="implicit declaration of function 'sleep' is invalid in C99"/>
        #    <error line="59" column="5" severity="warning" message="implicit declaration of function 'write' is invalid in C99"/>
        #    <error line="60" column="5" severity="warning" message="implicit declaration of function 'lseek' is invalid in C99"/>
        #    <error line="78" column="7" severity="warning" message="implicit declaration of function 'read' is invalid in C99"/>
        #   </file>
        #  </checkstyle>
        # <?xml version="1.0" encoding="utf-8"?>
        # <progress index="1" total="1"></progress>
        #
        # So we need to split xml chunks somehow
        # Will start by looking for opening tag (<checkstyle, <progress)
        # and parse accumulated xml when we encounter closing tag
        # TODO deal with < /> style tags
        rgxp = re.compile(r'<(\w+)')
        buffer = ''  # xml to be parsed
        start_tag = ''
        for line in iter(self.p.stdout.readline, b''):
            line = line.decode('utf-8')
            self.p.poll()

            if not start_tag:
                start_tag = re.findall(rgxp, line)
                start_tag = start_tag[0] if len(start_tag) else ''
            buffer += line
            if '</{}>'.format(start_tag) in line:
                tree = etree.fromstring(buffer)
                # OK, we received some chunk
                # check if it is progress update
                if (tree.tag == 'progress' and
                        tree.attrib['index'] == tree.attrib['total'] and
                        navigation_helper.flag == NavigationHelper.NAVIGATION_REQUESTED):
                    # notify about event
                    sublime.set_timeout(self.notify, 10)

                if fixits and (tree.tag == 'checkstyle'):
                    errors = []
                    key = 0
                    for file in tree.findall('file'):
                        for error in file.findall('error'):
                            if (error.attrib["severity"] == "fixit" or
                                error.attrib["severity"] == "error" or
                                error.attrib["severity"] == "warning"):
                                errdict = {}
                                errdict['key'] = key
                                errdict['file'] = file.attrib["name"]
                                errdict['severity'] = error.attrib["severity"]
                                errdict['line'] = int(error.attrib["line"])
                                errdict['column'] = int(error.attrib["column"])
                                if 'length' in error.attrib:
                                    errdict['length'] = int(error.attrib["length"])
                                else:
                                    errdict['length'] = -1
                                errdict['message'] = error.attrib["message"]
                                errors.append(errdict)
                                key = key + 1
                    sublime.active_window().active_view().run_command('rtags_fixit', {'errors': errors})
                buffer = ''
                start_tag = ''
        self.p = None

    def stop(self):
        if self.is_alive():
            self.p.kill()
            self.p = None


class CompletionJob():
    p = None

    def run(self, completion_job_id, filename, text, size, row, col):
        self.p = None

        switches = []

        # rc itself
        switches.append(RC_PATH)

        # auto-complete switch
        switches.append('-l')

        # the query
        switches.append('{}:{}:{}'.format(filename, row + 1, col + 1))

        switches.append('--unsaved-file')

        # We launch rc utility with both filename:line:col and filename:length
        # because we're using modified file which is passed via stdin (see --unsaved-file
        # switch)
        switches.append('{}:{}'.format(filename, size))

        switches.append('--synchronous-completions')

        self.p = subprocess.Popen(
            switches,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE)

        out, err = self.p.communicate(input=text, timeout=rc_timeout)

        suggestions = []
        for line in out.splitlines():
            # line is like this
            # "process void process(CompletionThread::Request *request) CXXMethod"
            # "reparseTime int reparseTime VarDecl"
            # "dump String dump() CXXMethod"
            # "request CompletionThread::Request * request ParmDecl"
            # we want it to show as process()\tCXXMethod
            #
            # output is list of tuples: first tuple element is what we see in popup menu
            # second is what inserted into file. '$0' is where to place cursor.
            # TODO play with $1, ${2:int}, ${3:string} and so on
            elements = line.decode('utf-8').split()
            suggestions.append(('{}\t{}'.format(' '.join(elements[1:-1]), elements[-1]),
                                '{}$0'.format(elements[0])))

        self.p = None

        return (completion_job_id, suggestions)

    def stop(self):
        self.p.kill()
        self.p = None


class FixitsController():

    def __init__(self):
        self.regions = []

    def clear_regions(self, view):
        for region in self.regions:
            view.erase_regions(region['key'])

    def show_regions(self, view):
        for region in self.regions:
            view.add_regions(region['key'], [region['region']], "string", "cross")

    def hover_region(self, view, point):
        for region in self.regions:
            if region['region'].contains(point):
                return region
        return None

    def cursor_region(self, view, row, col):
        if not row:
            return None

        start = view.text_point(row, 0)
        end = view.line(start).b
        cursor_region = sublime.Region(start, end)

        for region in self.regions:
            if cursor_region.contains(region['region']):
                return region

        return None

    def show_fixit(self, view, region):
        view.show_popup(
            "<nbsp/>{}<nbsp/>".format(region['message']),
            sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            region['region'].a)

    def update_regions(self, view, errors):

        def errors_to_regions(error):
            start = view.text_point(error['line']-1, error['column']-1)

            if error['length'] > 0:
                end = view.text_point(error['line']-1, error['column']-1 + error['length'])
            else:
                end = view.line(start).b

            return {
            "key": "RTagsErrorKey{}".format(error['key']),
            "region": sublime.Region(start, end),
            "message": error['message']}

        self.regions = list(map(errors_to_regions, errors))


reg = r'(\S+):(\d+):(\d+):(.*)'


class RtagsBaseCommand(sublime_plugin.TextCommand):

    def run(self, edit, switches, *args, **kwargs):
        # do nothing if not called from supported code
        if not supported_file_type(self.view):
            return
        # file should be reindexed only when
        # 1. file buffer is dirty (modified)
        # 2. there is no pending reindexation (navigation_helper flag)
        # 3. current text is different from previous one
        # It takes ~40-50 ms to reindex 2.5K C file and
        # miserable amount of time to check text difference
        if (navigation_helper.flag == NavigationHelper.NAVIGATION_DONE and
                self.view.is_dirty() and
                navigation_helper.data != get_view_text(self.view)):
            navigation_helper.switches = switches
            navigation_helper.data = get_view_text(self.view)
            navigation_helper.flag = NavigationHelper.NAVIGATION_REQUESTED
            self._reindex(self.view.file_name())
            # never go further
            return

        out, err = run_rc(switches, None, True, self._query(*args, **kwargs))
        # dirty hack
        # TODO figure out why rdm responds with 'Project loading'
        # for now just repeat query
        if out == b'Project loading\n':
            def rerun():
                self.view.run_command('rtags_location', {'switches': switches})
            sublime.set_timeout_async(rerun, 500)
            return

        # drop the flag, we are going to navigate
        navigation_helper.flag = NavigationHelper.NAVIGATION_DONE
        navigation_helper.switches = []

        self._action(out, err)

    def _reindex(self, filename):
        run_rc(['-V'], get_view_text(self.view), True, filename,
               '--unsaved-file', '{}:{}'.format(filename, self.view.size()))

    def on_select(self, res):
        if res == -1:
            return
        (file, line, col, _) = re.findall(reg, self.last_references[res])[0]
        nrow, ncol = self.view.rowcol(self.view.sel()[0].a)
        navigation_helper.history.append(
            (self.view.file_name(), nrow + 1, ncol + 1))
        if len(navigation_helper.history) > int(settings.get('jump_limit', 10)):
            navigation_helper.history.popleft()
        view = self.view.window().open_file(
            '%s:%s:%s' % (file, line, col), sublime.ENCODED_POSITION)

    def on_highlight(self, res):
        if res == -1:
            return
        (file, line, col, _) = re.findall(reg, self.last_references[res])[0]
        nrow, ncol = self.view.rowcol(self.view.sel()[0].a)
        view = self.view.window().open_file(
            '%s:%s:%s' % (file, line, col), sublime.ENCODED_POSITION | sublime.TRANSIENT)

    def _query(self, *args, **kwargs):
        return ''

    def _validate(self, stdout, stderr):
        # check if the file in question is not indexed by rtags
        if stdout == b'Not indexed\n':
            self.view.show_popup("<nbsp/>Not indexed<nbsp/>")
            return False
        # check if rtags is actually running
        if stdout.decode('utf-8').startswith("Can't seem to connect to server"):
            self.view.show_popup("<nbsp/>{}<nbsp/>".format(stdout.decode('utf-8')))
            return False
        return True

    def _action(self, stdout, stderr):
        if not self._validate(stdout, stderr):
            return

        # pretty format the results
        items = list(map(lambda x: x.decode('utf-8'), stdout.splitlines()))
        self.last_references = items
        def out_to_items(item):
            (file, line, _, usage) = re.findall(reg, item)[0]
            return [usage.strip(), "{}:{}".format(file.split('/')[-1], line)]
        items = list(map(out_to_items, items))

        # if there is only one result no need to show it to user
        # just do navigation directly
        if len(items) == 1:
            self.on_select(0)
            return
        # else show all available options
        self.view.window().show_quick_panel(
            items, self.on_select, sublime.MONOSPACE_FONT, -1, self.on_highlight)


class RtagsFixitCommand(RtagsBaseCommand):

    def run(self, edit, **args):
        fixits_controller.clear_regions(self.view)
        fixits_controller.update_regions(self.view, args["errors"])
        fixits_controller.show_regions(self.view)


class RtagsGoBackwardCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        try:
            file, line, col = navigation_helper.history.pop()
            view = self.view.window().open_file(
                '%s:%s:%s' % (file, line, col), sublime.ENCODED_POSITION)
        except IndexError:
            pass


class RtagsLocationCommand(RtagsBaseCommand):

    def _query(self, *args, **kwargs):
        row, col = self.view.rowcol(self.view.sel()[0].a)
        return '{}:{}:{}'.format(self.view.file_name(),
                                 row + 1, col + 1)


class RtagsSymbolInfoCommand(RtagsLocationCommand):
    panel_name = 'cursor'
    inforeg = r'(\S+):\s*(.+)'

    def filter_items(self, item):
        return re.match(self.inforeg, item)

    def _action(self, out, err):
        if not self._validate(out, err):
            return

        items = list(map(lambda x: x.decode('utf-8'), out.splitlines()))
        items = list(filter(self.filter_items, items))
        def out_to_items(item):
            (title, info) = re.findall(self.inforeg, item)[0]
            return [info.strip(), title.strip()]
        items = list(map(out_to_items, items))
        self.last_references = items

        self.view.window().show_quick_panel(
            items,
            None,
            sublime.MONOSPACE_FONT,
            -1,
            None)


class RtagsNavigationListener(sublime_plugin.EventListener):

    def cursor_pos(self, view, pos=None):
        if not pos:
            pos = view.sel()
            if len(pos) < 1:
                # something is wrong
                return None
            # we care about the first position
            pos = pos[0].a
        return view.rowcol(pos)

    def on_hover(self, view, point, hover_zone):
        region = fixits_controller.hover_region(view, point)
        if region:
            fixits_controller.show_fixit(view, region)

    def on_selection_modified(self, view):
        (row, col) = self.cursor_pos(view)
        region = fixits_controller.cursor_region(view, row, col)
        if region:
            fixits_controller.show_fixit(view, region)

    def on_post_save(self, view):
        # do nothing if not called from supported code
        if not supported_file_type(view):
            return

        # run rc --check-reindex to reindex just saved files
        run_rc(['-x'], None, True, view.file_name())

        # do nothing if we dont want to support fixits
        if not fixits:
            return

        # check if we are currently indexing
        out, err = run_rc(['--is-indexing', '--silent-query'], None, False)
        if out.decode().strip() == "1":
            progress_indicator.set_busy(True)

    def on_post_text_command(self, view, command_name, args):
        # do nothing if not called from supported code
        if not supported_file_type(view):
            return
        # if view get 'clean' after undo check if we need reindex
        if command_name == 'undo' and not view.is_dirty():
            run_rc(['-V'], None, False, view.file_name())


class RtagsCompleteListener(sublime_plugin.EventListener):
    # TODO refactor
    suggestions = []
    completion_job_id = None
    pool = futures.ThreadPoolExecutor(max_workers=1)
    lock = RLock()

    def completion_done(self, future):
        if not future.done():
            return;

        (completion_job_id, suggestions) = future.result()

        if completion_job_id != self.completion_job_id:
            return;

        self.suggestions = suggestions

        view = sublime.active_window().active_view()

        # hide the completion we might currently see as those are sublime's
        # own completions
        view.run_command('hide_auto_complete')

        # trigger a new completion event to show the freshly acquired ones
        view.run_command('auto_complete', {
            'disable_auto_insert': True,
            'api_completions_only': False,
            'next_competion_if_showing': False})

    def on_query_completions(self, view, prefix, location):
        # check if autocompletion was disabled for this plugin
        if not auto_complete:
            return []

        # do nothing if not called from supported code
        if not supported_file_type(view):
            return []

        # libclang does auto-complete _only_ at whitespace and punctuation chars
        # so "rewind" location to that character
        trigger_position = location[0] - len(prefix)

        completion_job_id = "CompletionJobId{}".format(trigger_position)

        # if we have a completion for this position, show that
        if self.completion_job_id == completion_job_id:
            return self.suggestions, sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS

        # we need to trigger a new completion
        job = CompletionJob()
        self.completion_job_id = completion_job_id
        text = get_view_text(view)
        row, col = view.rowcol(trigger_position)
        filename = view.file_name()
        size = view.size()

        with RtagsCompleteListener.lock:
            future = self.pool.submit(job.run, completion_job_id, filename, text, size, row, col)
            future.add_done_callback(self.completion_done)

        return ([], sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)


def update_settings():
    globals()['settings'] = sublime.load_settings(
        'RtagsComplete.sublime-settings')
    globals()['RC_PATH'] = settings.get('rc_path', 'rc')
    globals()['rc_timeout'] = settings.get('rc_timeout', 0.5)
    globals()['auto_complete'] = settings.get('auto_complete', True)
    globals()['fixits'] = settings.get('fixits', False)


def init():
    update_settings()

    globals()['navigation_helper'] = NavigationHelper()
    globals()['rc_thread'] = RConnectionThread()
    globals()['progress_indicator'] = ProgressIndicator()
    globals()['fixits_controller'] = FixitsController()

    rc_thread.start()

    settings.add_on_change('rc_path', update_settings)
    settings.add_on_change('rc_timeout', update_settings)
    settings.add_on_change('auto_complete', update_settings)
    settings.add_on_change('fixits', update_settings)


def plugin_loaded():
    sublime.set_timeout(init, 200)


def plugin_unloaded():
    # stop `rc -m` thread
    sublime.set_timeout(rc_thread.stop, 100)
