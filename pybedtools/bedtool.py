import tempfile
from math import floor, ceil
import os
import random
import string

from pybedtools.helpers import _file_or_bedtool, _help, _implicit,\
    _returns_bedtool, get_tempdir, _tags,\
    History, HistoryStep, call_bedtools, _flatten_list, IntervalIterator

from cbedtools import IntervalFile
import pybedtools


class BedTool(object):
    TEMPFILES = []

    def __init__(self, fn, from_string=False):
        """
        Wrapper around Aaron Quinlan's ``BEDtools`` suite of programs
        (https://github.com/arq5x/bedtools); also contains many useful
        methods for more detailed work with BED files.

        *fn* is a BED format file, or alternatively another BedTool instance.

        If *from_string* is True, then treat all spaces as TABs and write to
        tempfile, treating whatever you pass as *fn* as the contents of the bed
        file.  This also strips empty lines.

        Typical usage is to point to an existing file::


            a = BedTool('a.bed')

        But you can also create one from scratch from a string::

            >>> s = '''
            ... chrX  1  100
            ... chrX 25  800
            ... '''
            >>> a = BedTool(s,from_string=True).saveas('a.bed')

        Or use examples that come with pybedtools::

             >>> example_files = pybedtools.list_example_files()
             >>> assert example_files[0] == 'a.bed'
             >>> a = pybedtools.example_bedtool('a.bed')

        """
        if not from_string:
            if isinstance(fn, BedTool):
                fn = fn.fn
            elif isinstance(fn, basestring):
                if not os.path.exists(fn):
                    raise ValueError('File "%s" does not exist' % fn)
            else:
                fn = fn
        else:
            bed_contents = fn
            fn = self._tmp()
            fout = open(fn, 'w')
            for line in bed_contents.splitlines():
                if len(line.strip()) == 0:
                    continue
                line = '\t'.join(line.split()) + '\n'
                fout.write(line)
            fout.close()

        tag = ''.join([random.choice(string.lowercase) for _ in xrange(8)])
        self._tag = tag
        _tags[tag] = self
        self.fn = fn
        self._hascounts = False

        self.history = History()

    def delete_temporary_history(self, ask=True, raw_input_func=None):
        """
        Use at your own risk!  This method will delete temp files. You will be
        prompted for deletion of files unless you specify *ask=False*.

        Deletes all temporary files created during the history of this BedTool
        up to but not including the file this current BedTool points to.

        Any filenames that are in the history and have the following pattern
        will be deleted::

            <TEMP_DIR>/pybedtools.*.tmp

        (where <TEMP_DIR> is the result from get_tempdir() and is by default
        "/tmp")

        Any files that don't have this format will be left alone.

        (*raw_input_func* is used for testing)
        """
        flattened_history = _flatten_list(self.history)
        to_delete = []
        tempdir = get_tempdir()
        for i in flattened_history:
            fn = i.fn
            if fn.startswith(os.path.join(os.path.abspath(tempdir),
                                          'pybedtools')):
                if fn.endswith('.tmp'):
                    to_delete.append(fn)

        if raw_input_func is None:
            raw_input_func = raw_input

        str_fns = '\n\t'.join(to_delete)
        if ask:
            answer = raw_input_func('Delete these files?\n\t%s\n(y/N) ' \
                                    % str_fns)

            if not answer.lower()[0] == 'y':
                print('OK, not deleting.')
                return
        for fn in to_delete:
            os.unlink(fn)
        return

    def _log_to_history(method):
        """
        Decorator to add a method and its kwargs to the history.

        Assumes that you only add this decorator to bedtool instances that
        return other bedtool instances
        """
        def decorated(self, *args, **kwargs):

            # this calls the actual method in the first place; *result* is
            # whatever you get back
            result = method(self, *args, **kwargs)

            # add appropriate tags
            parent_tag = self._tag
            result_tag = result._tag

            # log the sucka
            history_step = HistoryStep(method, args, kwargs, self, parent_tag,
                                       result_tag)

            # only add the current history to the new bedtool if there's
            # something to add
            if len(self.history) > 0:
                result.history.append(self.history)

            # but either way, add this history step to the result.
            result.history.append(history_step)

            return result

        decorated.__doc__ = method.__doc__
        return decorated

    def filter(self, func, *args, **kwargs):
        """
        Takes a function *func* that is called for each feature
        in the `BedTool` object and returns only those
        for which the function returns True.

        *args and **kwargs are passed directly to *func*.

        Returns a streaming BedTool; if you want the filename then use the
        .saveas() method.

        >>> a = pybedtools.example_bedtool('a.bed')
        >>> subset = a.filter(lambda b: b.chrom == 'chr1' and b.start < 150)
        >>> len(a), len(subset)
        (4, 2)

        so it has extracted 2 records from the original 4.

        """
        return BedTool((f for f in self if func(f, *args, **kwargs)))

    def field_count(self, n=10):
        """
        Return the number of fields in the features this file contains.  Checks
        the first *n* features.
        """
        i = 0
        fields = set([])
        for feat in self:
            if i > n:
                break
            i += 1
            # TODO: make this more efficient.
            fields.update([len(feat.fields)])
        assert len(fields) == 1, fields
        return list(fields)[0]

    def each(self, func, *args, **kwargs):
        """
        Applies user-defined function *func* to each feature.  *func* must
        accept an Interval as its first argument; *args and **kwargs will be
        passed to *func*.

        *func* must return an Interval object.

        >>> def truncate_feature(feature, limit=0):
        ...     feature.score = str(len(feature))
        ...     if len(feature) > limit:
        ...         feature.stop = feature.start + limit
        ...         feature.name = feature.name + '.short'
        ...     return feature

        >>> a = pybedtools.example_bedtool('a.bed')
        >>> b = a.each(truncate_feature, limit=100)
        >>> print b #doctest: +NORMALIZE_WHITESPACE
        chr1	1	100	feature1	99	+
        chr1	100	200	feature2	100	+
        chr1	150	250	feature3.short	350	-
        chr1	900	950	feature4	50	+
        <BLANKLINE>

        """
        fh = open(self._tmp(), "w")
        for f in self:
            new_feature = func(f, *args, **kwargs)
            print >>fh, new_feature
        fh.close()
        return BedTool(fh.name)

    @property
    def file_type(self):
        """
        Return the type of the current file.  One of ('bed','vcf','gff').

        >>> a = pybedtools.example_bedtool('a.bed')
        >>> a.file_type
        'bed'
        """

        v = IntervalFile(self.fn)
        return v.file_type

    def cut(self, indexes):
        """
        Similar to unix `cut` except indexes are 0-based, must be a list
        and the columns are returned in the order requested.

        In addition, indexes can contain keys of the GFF/GTF attributes,
        in which case the values are returned. e.g. 'gene_name' will return the
        corresponding name from a GTF, or 'start' will return the start
        attribute of a BED Interval.

        See .with_column() if you need to do more complex operations.
        """
        fh = open(self._tmp(), "w")
        for f in self:
            print >>fh, "\t".join(map(str, [f[attr] for attr in indexes]))
        fh.close()
        return BedTool(fh.name)

    def _tmp(self):
        '''
        Makes a tempfile and registers it in the BedTool.TEMPFILES class
        variable.  Adds a "pybedtools." prefix and ".tmp" extension for easy
        deletion if you forget to call pybedtools.cleanup().
        '''
        tmpfn = tempfile.NamedTemporaryFile(prefix='pybedtools.',
                                            suffix='.tmp', delete=False)
        tmpfn = tmpfn.name
        BedTool.TEMPFILES.append(tmpfn)
        return tmpfn

    def __iter__(self):
        '''Iterator that returns lines from BED file'''
        if isinstance(self.fn, basestring):
            return IntervalFile(self.fn)

        if isinstance(self.fn, file):
            return IntervalIterator(self.fn)

        else:
            return self.fn

    def __repr__(self):
        if isinstance(self.fn, file):
            return '<BedTool(stream)>'
        if isinstance(self.fn, basestring):
            if os.path.exists(self.fn):
                return '<BedTool(%s)>' % self.fn
            else:
                return '<BedTool(MISSING FILE: %s)>'%self.fn
        else:
            return repr(self.fn)

    def __str__(self):
        if isinstance(self.fn, basestring):
            f = open(self.fn)
            s = f.read()
            f.close()
            return s
        else:
            return '\n'.join(str(i) for i in self)+'\n'

    def __len__(self):
        return self.count()

    def __eq__(self, other):
        if isinstance(self.fn, file) or isinstance(other.fn, file):
            raise NotImplementedError('Testing equality not supported for streams')
        if open(self.fn).read() == open(other.fn).read():
            return True
        return False

    def __ne__(self, other):
        if open(self.fn).read() == open(other.fn).read():
            return False
        return True

    @_file_or_bedtool()
    def __add__(self,other):
        return self.intersect(other, u=True)

    @_file_or_bedtool()
    def __sub__(self, other):
        return self.intersect(other, v=True)

    def head(self, n=10):
        """
        Prints the first *n* lines
        """
        for i,line in enumerate(open(self.fn)):
            if i == (n):
                break
            print line,

    def set_chromsizes(self, chromsizes):
        """
        Set the chromsizes for this genome.

        Example usage::

            >>> hg19 = pybedtools.chromsizes('hg19')
            >>> a = pybedtools.example_bedtool('a.bed')
            >>> a = a.set_chromsizes(hg19)
            >>> print a.chromsizes['chr1']
            (1, 249250621)

            >>> # Now you can use things like pybedtools_shuffle
            >>> b = a.pybedtools_shuffle()
        """
        self.chromsizes = chromsizes
        return self

    @_help('intersectBed')
    @_file_or_bedtool()
    @_implicit('-a')
    @_returns_bedtool()
    @_log_to_history
    def intersect(self, b=None, **kwargs):
        """
        Intersect with another BED file. If you want to use BAM as input, you
        need to specify *abam='filename.bam'*.  Returns a new BedTool object.

        Example usage:

            Create new BedTool object

            >>> a = pybedtools.example_bedtool('a.bed')

            Get overlaps with `b.bed`:

            >>> b = pybedtools.example_bedtool('b.bed')
            >>> overlaps = a.intersect(b)

            Use `v=True` to get the inverse -- that is, those unique to "a.bed":

            >>> unique_to_a = a.intersect(b, v=True)
        """

        other = b
        if 'b' not in kwargs:
            if isinstance(other, basestring):
                kwargs['b'] = other
            else:
                assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'
                kwargs['b'] = other.fn

        if ('abam' not in kwargs) and ('a' not in kwargs):
            if isinstance(self.fn, basestring):
                kwargs['a'] = self.fn
            if isinstance(self.fn, file):
                kwargs['-'] = self.fn
        try:
            if kwargs.pop('stream'):
                tmp = None
        except KeyError:
            tmp = self._tmp()

        cmds = ['intersectBed',]
        cmds.extend(self.parse_kwargs(**kwargs))


        stream = call_bedtools(cmds, tmp, stdin=self.fn)

        other = BedTool(stream)

        # tag the new BedTool as having counts
        if 'c' in kwargs:
            other._hascounts = True

        return other

    @_help('fastaFromBed')
    @_implicit('-bed')
    @_returns_bedtool()
    def sequence(self, **kwargs):
        '''
        Wraps ``fastaFromBed``.  *fi* is passed in by the user; *bed* is
        automatically passed in as the bedfile of this object; *fo* by default
        is a temp file.  Use save_seqs() to save as a file.

        The end result is that this BedTool will have an attribute, self.seqfn,
        that points to the new fasta file.

        Example usage::

            a = pybedtools.example_bedtool('a.bed')
            a.sequence(fi='genome.fa')
            a.print_sequence()
        '''
        if 'bed' not in kwargs:
            kwargs['bed'] = self.fn

        tmp = self._tmp()
        if 'fo' not in kwargs:
            kwargs['fo'] = tmp

        def check_sequence_stderr(x):
            if x.startswith('index file'):
                return True
            return False

        cmds = ['fastaFromBed']
        cmds.extend(self.parse_kwargs(**kwargs))
        call_bedtools(cmds, tmp, check_stderr=check_sequence_stderr)
        self.seqfn = tmp
        return self

    @_help('subtractBed')
    @_file_or_bedtool()
    @_returns_bedtool()
    @_log_to_history
    def subtract(self, other, **kwargs):
        """
        Subtracts from another BED file and returns a new BedTool object.

        Example usage:

            >>> a = pybedtools.example_bedtool('a.bed')
            >>> b = pybedtools.example_bedtool('b.bed')

            Do a "stranded" subtraction:

            >>> c = a.subtract(b, s=True)

            Require 50% of features in `a` to overlap:

            >>> c = a.subtract(b, f=0.5)
        """
        if 'a' not in kwargs:
            kwargs['a'] = self.fn

        if 'b' not in kwargs:
            if isinstance(other, basestring):
                kwargs['b'] = other
            else:
                assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'
                kwargs['b'] = other.fn

        cmds = ['subtractBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    @_help('slopBed')
    @_implicit('-i')
    @_returns_bedtool()
    @_log_to_history
    def slop(self, **kwargs):
        """
        Wraps slopBed, which adds bp to each feature.  Returns a new BedTool
        object.

        If *g* is a dictionary (for example, return values from
        pybedtools.chromsizes() ) it will be converted to a temp file for use
        with slopBed.  If it is a string, then it is assumed to be a filename.

        Example usage:

            >>> a = pybedtools.example_bedtool('a.bed')

            Increase the size of features by 100 bp in either direction.  Note
            that you need to specify either a dictionary of chromsizes or a
            filename containing chromsizes for the genome that your bed file
            corresponds to:

            >>> c = a.slop(g=pybedtools.chromsizes('hg19'), b=100)

            Grow features by 10 bp upstream and 500 bp downstream, using a
            genome file you already have constructed called 'hg19.genome'

            First, create the file:

            >>> fout = open('hg19.genome','w')
            >>> chromdict = pybedtools.get_chromsizes_from_ucsc('hg19')
            >>> for chrom, size in chromdict.items():
            ...     fout.write("%s\\t%s\\n" % (chrom, size[1]))
            >>> fout.close()

            Then use it:

            >>> c = a.slop(g='hg19.genome', l=10, r=500, s=True)

            Clean up afterwards:

            >>> os.unlink('hg19.genome')

        """
        if 'i' not in kwargs:
            kwargs['i'] = self.fn

        if 'g' not in kwargs:
            try:
                kwargs['g'] = self.chromsizes

            except AttributeError:
                raise ValueError('No genome specified. Either pass a "g" argument or use set_chromsizes()')

        # If it's a dictionary, then convert to file and overwrite kwargs['g'].
        if isinstance(kwargs['g'], dict):
            genome_fn = self._tmp()
            pybedtools.chromsizes_to_file(kwargs['g'], genome_fn)
            kwargs['g'] = genome_fn

        cmds = ['slopBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    @_help('mergeBed')
    @_implicit('-i')
    @_returns_bedtool()
    @_log_to_history
    def merge(self, **kwargs):
        """
        Merge overlapping features together. Returns a new BedTool object.

        Example usage:

            >>> a = pybedtools.example_bedtool('a.bed')

            Merge:

            >>> c = a.merge()

            Allow merging of features 500 bp apart:

            >>> c = a.merge(d=500)

            Report number of merged features:

            >>> c = a.merge(n=True)

            Report names of merged features:

            >>> c = a.merge(nms=True)

        """
        if 'i' not in kwargs:
            kwargs['i'] = self.fn

        cmds = ['mergeBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    @_help('closestBed')
    @_file_or_bedtool()
    @_implicit('-a')
    @_returns_bedtool()
    @_log_to_history
    def closest(self, other, **kwargs):
        """
        Return a new BedTool object containing closest features in *other*.  Note
        that the resulting file is no longer a valid BED format; use the
        special "_closest" methods to work with the resulting file.

        Example usage::

            a = BedTool('in.bed')

            # get the closest feature in 'other.bed' on the same strand
            b = a.closest('other.bed', s=True)

        """
        if 'a' not in kwargs:
            kwargs['a'] = self.fn

        if 'b' not in kwargs:
            if isinstance(other, basestring):
                kwargs['b'] = other
            else:
                assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'
                kwargs['b'] = other.fn

        cmds = ['closestBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        newbedtool = BedTool(tmp)
        newbedtool.closest_output = True
        return newbedtool

    @_help('windowBed')
    @_file_or_bedtool()
    @_implicit('-a')
    @_log_to_history
    def window(self, other, **kwargs):
        """
        Intersect with a window.

        Example usage::

            a = BedTool('in.bed')

            # Consider features up to 500 bp away as overlaps
            b = a.window(w=500)
        """
        if 'a' not in kwargs:
            kwargs['a'] = self.fn
        if 'b' not in kwargs:
            if isinstance(other, basestring):
                kwargs['b'] = other
            else:
                assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'
                kwargs['b'] = other.fn

        cmds = ['windowBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    @_help('shuffleBed')
    @_implicit('-i')
    @_log_to_history
    def shuffle(self, genome=None, **kwargs):
        if genome is not None:
            genome_fn = pybedtools.chromsizes_to_file(pybedtools.get_chromsizes_from_ucsc(genome))
            kwargs['g'] = genome_fn
        if 'i' not in kwargs:
            kwargs['i'] = self.fn

        cmds = ['shuffleBed',]
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    @_help('sortBed')
    @_implicit('-i')
    @_log_to_history
    def sort(self,**kwargs):
        if 'i' not in kwargs:
            kwargs['i'] = self.fn

        cmds = ['sortBed']
        cmds.extend(self.parse_kwargs(**kwargs))
        tmp = self._tmp()
        call_bedtools(cmds, tmp)
        return BedTool(tmp)

    def features(self):
        """
        Returns an iterator of :class:`feature` objects.
        """
        return iter(self)

    def count(self):
        """
        Number of features in BED file. Does the same thing as len(self), which
        actually just calls this method.

        Only counts the actual features.  Ignores any track lines, browser
        lines, lines starting with a "#", or blank lines.

        Example usage::

            a = BedTool('in.bed')
            a.count()
        """
        return sum(1 for _ in self)

    def print_sequence(self):
        """
        Print the sequence that was retrieved by the :meth:`BedTool.sequence`
        method.

        See usage example in :meth:`BedTool.sequence`.
        """
        if not hasattr(self,'seqfn'):
            raise ValueError, 'Use .sequence(fasta_fn) to get the sequence first'
        f = open(self.seqfn)
        s = f.read()
        f.close()
        return s

    def save_seqs(self,fn):
        """
        Save sequences of features in this BedTool object as a fasta file *fn*.

        In order to use this function, you need to have called
        the :meth:`BedTool.sequence()` method.

        A new BedTool object is returned which references the newly saved file.

        Example usage::

            a = BedTool('in.bed')

            # specify the filename of the genome in fasta format
            a.sequence('data/genomes/genome.fa')

            # use this method to save the seqs that correspond to the features
            # in "a"
            a.save_seqs('seqs.fa')
        """
        if not hasattr(self,'seqfn'):
            raise ValueError, 'Use .sequence(fasta_fn) to get the sequence first'
        fout = open(fn,'w')
        fout.write(open(self.seqfn).read())
        fout.close()
        return BedTool(fn)

    def pybedtools_shuffle(self):
        """
        Quite fast implementation of shuffleBed; assumes shuffling within chroms.

        You need to call self.set_chromsizes() to tell this BedTool object what the
        chromosome sizes are that you want to shuffle within.

        Example usage::

            from pybedtools.genome_registry import hg19

            a = BedTool('in.bed')
            a.set_chromsizes(pybedtools.chromsizes('dm3'))

            # randomly shuffled version of "a"
            b = a.newshuffle()

        Alternatively, you can use a custom genome to shuffle within -- perhaps
        the regions probed by a tiling array::

            a = BedTool('in.bed')
            array_extent = {'chr11': (500000, 1100000),
                            'chr5': (1, 14000)}
            a.set_chromsizes(array_extent)
            b = a.pybedtools_shuffle()

        This is equivalent to the following command-line usage of ``shuffleBed``::

            shuffleBed -i in.bed -g dm3.genome -chrom -seed $RANDOM > /tmp/tmpfile

        """
        if not hasattr(self, 'chromsizes'):
            raise AttributeError, "Please use the set_chromsizes() method of this instance before randomizing"

        tmp = self._tmp()
        TMP = open(tmp,'w')
        for f in self:
            length = f.stop-f.start
            newstart = random.randint(self.chromsizes[f.chrom][0], self.chromsizes[f.chrom][1]-length)
            f.stop = newstart + length

            # Just overwrite start and stop, leaving the rest of the line in
            # place
            f.start = newstart
            TMP.write(str(f)+'\n')
        TMP.close()
        return BedTool(tmp)

    def randomstats(self, other, iterations, intersectkwargs=None):
        """
        Sends args to :meth:`BedTool.randomintersection` and compiles results
        into a dictionary with useful stats.  Requires scipy and numpy.

        Example usage::

            a = BedTool('in.bed')

            # Randomization results from 100 iterations, using the u=True kwarg (report
            # features in "a" only once for each intersection).
            results = a.randomstats('other.bed', iterations=100, intersectkwargs={'u':True})
        """
        try:
            from scipy import stats
            import numpy as np
        except ImportError:
            raise ImportError, "Need to install NumPy and SciPy for stats..."

        if isinstance(other, basestring):
            other = BedTool(other)
        else:
            assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'

        # Actual (unshuffled) counts.
        actual = len(self.intersect(other,**intersectkwargs))

        # List of counts from randomly shuffled versions.  Length of counts == *iterations*.
        distribution = self.randomintersection(other, iterations=iterations, intersectkwargs=intersectkwargs)
        distribution = np.array(distribution)

        # Median of distribution
        med_count = np.median(distribution)

        n = float(len(distribution))

        frac_above = sum(distribution >= actual)/n
        frac_below = sum(distribution <= actual)/n

        normalized = actual/med_count

        lower_thresh = 2.5
        upper_thresh = 97.5
        lower = stats.scoreatpercentile(distribution, lower_thresh)
        upper = stats.scoreatpercentile(distribution, upper_thresh)

        actual_percentile = stats.percentileofscore(distribution,actual)
        d = {
        'iterations':iterations,
        'actual': actual,
        'file_a':self.fn,
        'file_b':other.fn,
        self.fn: len(self),
        other.fn: len(other),
        'self':len(self),
        'other':len(other),
        'frac randomized above actual': frac_above,
        'frac randomized below actual': frac_below,
        'median randomized': med_count,
        'normalized': normalized,
        'lower_%sth'%lower_thresh: lower,
        'upper_%sth'%upper_thresh: upper,
        'percentile': actual_percentile,
        }
        return d

    def print_randomstats(self, other, iterations, intersectkwargs=None):
        """
        Nicely prints the reciprocal randomization of two files.
        """
        if (type(other) is str) or (type(other) is unicode):
            other = BedTool(other)

        d1 = self.randomstats(other, iterations, intersectkwargs)
        d2 = other.randomstats(self, iterations, intersectkwargs)

        s = '\n'
        s += 'Randomizing %s:' % self.fn
        s += '\t%s features in %s' % (d1[self.fn],self.fn)
        s += '\t%s features in %s' % (d1[other.fn],other.fn)
        s += '\t%s actual intersections' % d1['actual']
        s += '\t%.2f median randomized' % d1['median randomized']
        s += '\t%.2f enrichment score' % d1['normalized']
        s += '\t%.2f percentile' % d1['percentile']
        s += '\n'
        s += 'Randomizing %s:' % other.fn
        s += '\t%s features in %s' % (d2[other.fn],other.fn)
        s += '\t%s features in %s' % (d2[self.fn],self.fn)
        s += '\t%s actual intersection count' % d2['actual']
        s += '\t%.2f median randomized' % d2['median randomized']
        s += '\t%.2f enrichment score' % d2['normalized']
        s += '\t%.2f percentile' % d2['percentile']

        return s

    def randomintersection(self, other, iterations, **kwargs):
        """
        Performs *iterations* shufflings of self, each time intersecting with
        *other*.

        Returns a list of integers where each integer is the number of
        intersections of one shuffled file with *other*; this distribution can
        be used in downstream analysis for things like empirical p-values.

        *intersectkwargs* is a dictionary of kwargs to be passed to
        self.intersect().  By default, intersectkwargs=dict(u=True).
        Example usage::

            r = BedTool('in.bed').randomintersection('other.bed', 100)
        """
        # TODO: do we need this function?
        if kwargs == {}: kwargs['u'] = True
        for i in range(iterations):
            tmp = self.pybedtools_shuffle()
            tmp2 = tmp.intersect(other, **kwargs)
            yield len(tmp2)
            os.unlink(tmp.fn)
            os.unlink(tmp2.fn)
            del(tmp)
            del(tmp2)

    @_file_or_bedtool()
    @_returns_bedtool()
    def cat(self, other, postmerge=True, **kwargs):
        """
        Concatenates two BedTool objects (or an object and a file) and does an
        optional post-merge of the features.

        Use *postmerge=False* if you want to keep features separate.

        TODO:

            currently truncates at BED3 format!

        kwargs are sent to :meth:`BedTool.merge`.

        Example usage::

            a = BedTool('in.bed')

            # concatenate and merge features together if they overlap and are
            # on the same strand
            b = a.cat('other.bed', s=True)
        """
        tmp = self._tmp()
        if (type(other) is str) or (type(other) is unicode):
            other = BedTool(other)
        else:
            assert isinstance(other, BedTool), 'Either filename or another BedTool instance required'
        TMP = open(tmp,'w')
        for f in self:
            TMP.write('%s\t%i\t%i\n' % (f.chrom, f.start, f.end))
        for f in other:
            TMP.write('%s\t%i\t%i\n' % (f.chrom, f.start, f.end))
        TMP.close()
        c = BedTool(tmp)
        if postmerge:
            d = c.merge(**kwargs)
            return d
        else:
            return c

    def tostring(self):
        '''
        Returns the BED file as a string.  You can also ``print`` the BedTool object
        to view its contents.

        Example usage::

            a = BedTool('in.bed')

            # this is one looong string which contains the entire file
            long_string = a.tostring()
        '''
        f = open(self.fn)
        s = f.read()
        f.close()
        return s

    @_returns_bedtool()
    def saveas(self, fn, trackline=None):
        """
        Save BED file as a new file, adding the optional *trackline* to the
        beginning.

        Returns a new BedTool for the newly saved file.

        A newline is automatically added to the trackline if it does not
        already have one.

        Example usage::

        >>> a = pybedtools.example_bedtool('a.bed')
        >>> b = a.random_subset(2)
        >>> len(b)
        2
        """
        fout = open(fn, 'w')
        if trackline is not None:
            fout.write(trackline.strip() + '\n')
        fout.write(self.tostring())
        fout.close()
        return BedTool(fn)

    @_file_or_bedtool()
    def intersection_report(self, other, basename=True, **kwargs):
        """
        Prints a report of the reciprocal intersections with another bed file
        or :class:`BedTool` object.

        If *basename* is True (default), only prints the basename of the file
        and not the whole path.

        a = BedTool('in.bed')
        a.intersection_report('other.bed')
        """
        if (type(other) is str) or (type(other) is unicode):
            other = BedTool(other)

        int1 = self.intersect(other, **kwargs).count()
        int2 = other.intersect(self.fn, **kwargs).count()

        count1 = self.count()
        count2 = other.count()

        self_fn = self.fn
        other_fn = other.fn

        if basename:
            self_fn = os.path.basename(self_fn)
            other_fn = os.path.basename(other_fn)

        print '%s\n\t%s total\n\t%s (%.1f%%) of these intersect %s' % (self_fn, count1,  int1,  (float(int1)/count1)*100, other_fn)
        print '%s\n\t%s total\n\t%s (%.1f%%) of these intersect %s' % (other_fn, count2,  int2, (float(int2)/count2)*100, self_fn)

    @_returns_bedtool()
    def random_subset(self,n):
        '''
        Returns a new bedtools object containing a random subset of the
        features in this subset.

        Example usage::

            a = BedTool('in.bed')

            # Choose 5 random features from 'in.bed'
            b = a.random_subset(5)

        '''
        idxs = range(len(self))
        random.shuffle(idxs)
        idxs = idxs[:n]

        tmpfn = self._tmp()
        tmp = open(tmpfn,'w')
        for i, f in enumerate(self):
            if i in idxs:
                tmp.write(str(f)+'\n')
        tmp.close()
        return BedTool(tmpfn)

    def sorted(self,col, reverse=None):
        '''Returns a new BedTool object, sorted by the column specified. col
        can be a list of columns.  BED columns that are ints (start, stop and
        value) will be sorted numerically; other columns will be
        alphabetical.

        reverse is a list of booleans, same length as col, specifying which
        fields to reverse-sort.

        TODO: currently multiple columns aren't working!

        a = BedTool('in.fn')
        b = a.sorted(col=2) # sort by start position
        c = a.sorted(col=5,reverse=True) # reverse sort on the values
        '''

        if type(col) is not list:
            col = [col]

        if reverse is None:
            reverse = [False for i in col]
        elif type(reverse) is not list:
            reverse = [reverse]

        assert len(reverse) == len(col), 'reverse must be same length as col'

        if len(col) > 1:
            raise NotImplementedError,'multi-column sort not yet working correctly'

        d = {1:'1,1',
             2:'2n,2n',
             3:'3n,3n',
             4:'4,4',
             5:'5n,5n'}

        tmp = self._tmp()
        cmds = ['sort']
        for c,r in zip(col,reverse):
            if r:
                cmds.append('-k '+d[c]+'r')
            else:
                cmds.append('-k '+d[c])
        cmds.append(self.fn)
        cmds.extend( ['>',tmp] )
        os.system(' '.join(cmds))
        return BedTool(tmp)

    def sequence_coverage(self):
        """
        Returns the total number of bases covered by this BED file.  Does a
        self.merge() first to remove potentially multiple-counting bases.

        Example usage::

            a = BedTool('in.bed')

            # total bp in genome covered by 'in.bed'
            total_bp = a.sequence_coverage()
        """
        b = self.merge()
        total_bp = 0
        for feature in b.features():
            total_bp += len(feature)
        return total_bp

    def parse_kwargs(self,**kwargs):
        """
        Given a set of keyword arguments, turns them into a command line-ready
        list of strings.  E.g., the kwarg dict::

            kwargs = dict(c=True,f=0.5)

        will be returned as::

            ['-c','-f','0.5']

        If there are symbols (e.g., "|"), then the parameter is quoted."
        """
        illegal_chars = '!@#$%^&*(),-;:.<>?/|[]{} \'\\\"'
        cmds = []

        # If we're aiming to use stdin, then the '-' needs to be first.
        try:
            kwargs.pop('-')
            cmds.append('-')
        except KeyError:
            pass

        for key,value in kwargs.items():
            # e.g., u=True --> -u
            if value is True:
                cmds.append('-'+key)
                continue

            # support for lists of items
            if (type(value) is tuple) or (type(value) is list):
                value = ','.join(map(str,value))

            # left over from os.system() calls; subprocess.Popen does the nice
            # parsing for you
            if type(value) is str:
                for i in illegal_chars:
                    if i in value:
                        value = '%s' % value
                        break

            # e.g., b='f.bed' --> ['-b', 'f.bed']
            cmds.append('-'+key)
            cmds.append(str(value))

        return cmds

    @_returns_bedtool()
    def feature_centers(self,n,report_smaller=True):
        '''
        Returns a new BedTool object with just the centers of size n extracted
        from this object's features.

        If *report_smaller* is True, then report features that are smaller than
        *n*.  Otherwise, ignore them.

        Example usage::

            a = BedTool('in.bed')

            # 5bp on either side of the center of each feature
            b = a.feature_centers(100)
        '''
        tmpfn = self._tmp()
        tmp = open(tmpfn,'w')
        for f in self:

            # if smaller than window size, decide whether to report it or not.
            if (f.stop - f.start) < n:
                if report_smaller:
                    tmp.write(str(f)+'\n')
                    continue
                else:
                    continue

            left = floor(n/2.0)
            right = ceil(n/2.0)
            midpoint = f.start + (f.stop - f.start)/2
            f.start = int(midpoint - left)
            f.end = int(midpoint + right)
            tmp.write(str(f)+'\n')
        tmp.close()
        return BedTool(tmpfn)

    @_returns_bedtool()
    def rename_features(self, new_name):
        """
        Forces a rename of all features.  Useful for if you have a BED file of
        exons and you want all of them to have the name "exon".
        """
        tmpfn = self._tmp()
        tmp = open(tmpfn, 'w')
        for f in self:
            f.name = new_name
            print >>tmp, str(f)
        tmp.close()
        return BedTool(tmpfn)

    @_returns_bedtool()
    def with_attrs(self, **kwargs):
        """
        Given arbitrary keyword arguments, turns the keys and values into
        attributes.

        Example usage::

            # add a "label" attribute to each BedTool
            a = BedTool('a.bed').with_attrs(label='transcription factor 1')
            b = BedTool('b.bed').with_attrs(label='transcription factor 2')
            for i in [a,b]:
                print i.count(), 'features for', i.label
        """
        for key,value in kwargs.items():
            setattr(self,key,value)
        return self

    def counts(self):
        """
        After running :meth:`BedTool.intersect` with the kwarg *c=True*, use
        this method to return a list of the count of features in "b" that
        intersected each feature in "a".

        Example usage::

            a = BedTool('in.bed')
            b = a.intersect('other.bed', c=True)
            counts = b.counts()

            # assuming you have matplotlib installed, plot a histogram

            import pylab
            pylab.hist(counts)
            pylab.show()
        """
        if not self._hascounts:
            raise ValueError, 'Need intersection counts; run intersection(fn, c=True) for this or manually set self._hascounts=True.'
        for f in self:
            yield f.count

    def normalized_counts(self):
        """
        After running :meth:`BedTool.intersect` with the kwarg *c=True*, use
        this method to return a list of the density of features in "b" that
        intersected each feature in "a".

        This takes the counts in each feature and divides by the bp in that
        feature.

        Example usage::

            a = BedTool('in.bed')

            # intersect, with c=True to get counts -- number of features in
            # 'other.bed' that intersect with features in a
            b = a.intersect('other.bed', c=True)

            # number of features in 'other.bed' found in each feature in "a",
            # divided by the size of the feature in "a"
            counts = b.normalized_counts()

            # assuming you have matplotlib installed, plot a histogram

            import pylab
            pylab.hist(counts)
            pylab.show()
        """
        if not self._hascounts:
            raise ValueError, 'Need intersection counts; run intersection(fn, c=True) for this or manually set self._hascounts=True.'
        for f in self:
            yield f.count / float(f.stop - f.start)

if __name__ == "__main__":
    print 'Running tests...'
    import doctest
    doctest.testmod(optionflags=doctest.NORMALIZE_WHITESPACE)
