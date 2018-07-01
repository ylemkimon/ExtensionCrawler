#
# Copyright (C) 2018 The University of Sheffield, UK
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import sys
import getopt
import os
import MySQLdb.cursors
from itertools import groupby
import time
from multiprocessing import Pool, cpu_count
from functools import partial


class SimhashBucket:
    """Implementation of http://wwwconference.org/www2007/papers/paper215.pdf"""

    def __init__(self, nr_of_tables):
        self.tables = nr_of_tables * [{}]

        # So far, we support the variants with 4 and 20 tables. Each element of splitters
        # describes the key for one table. The first element of the tuple indicates the number
        # of bits that we shift the simhash to the right; the second element indicates how many
        # bits, from the right side, we end up taking.
        if nr_of_tables == 4:
            self.splitters = [[(0, 16)], [(16, 16)], [(32, 16)], [(48, 16)]]
        elif nr_of_tables == 20:
            block_sizes = [11, 11, 11, 11, 10, 10]
            self.splitters = []
            for i in range(0, len(block_sizes)):
                for j in range(i + 1, len(block_sizes)):
                    for k in range(j + 1, len(block_sizes)):
                        self.splitters += [[
                            (sum(block_sizes[i+1:]), block_sizes[i]),
                            (sum(block_sizes[j+1:]), block_sizes[j]),
                            (sum(block_sizes[k+1:]), block_sizes[k]),
                        ]]
        else:
            raise Exception(f"Unsupported number of tables: {nr_of_tables}")


    def bit_count(self, n):
        return bin(n).count("1")

    def get_chunk(self, n, i):
        """Reduces the simhash to a small chunk, given by self.splitters. The chunk will
        then be compared exactly in order to increase performance."""
        sum = 0
        for (s, c) in self.splitters[i]:
            sum <<= c
            sum += (n >> s) & (pow(2, c) - 1)
        return sum

    def add(self, fp):
        for i, tbl in enumerate(self.tables):
            fp_chunk = self.get_chunk(fp[0], i)
            if not fp_chunk in tbl:
                tbl[fp_chunk] = []
            tbl[fp_chunk] += [fp]

    def query(self, q):
        for i, tbl in enumerate(self.tables):
            q_chunk = self.get_chunk(q, i)
            if q_chunk in tbl:
                for fp in tbl[q_chunk]:
                    diff = self.bit_count(q ^ fp[0])
                    if diff < 4:
                        yield (fp, diff)

    def addMany(self, fps):
        for fp in fps:
            self.add(fp)

    def queryMany(self, qs):
        for q in qs:
            for (fp, diff) in self.query(q):
                yield (fp, diff)


def get_first(x):
    return x[0]


def get_cdnjs_simhashes(db, limit=None):
    db.execute("select simhash, library from cdnjs where "
                "simhash IS NOT NULL AND path like '%.js' and "
                "HEX(md5) <> 'd41d8cd98f00b204e9800998ecf8427e'" +
                (f" LIMIT {int(limit)}" if limit is not None else ""))

    for row in db.fetchall():
        row["simhash"] = int(row["simhash"])
        yield row


def get_crxfile_simhashes(db, limit=None):
    db.execute("select crx_etag, path, simhash from crxfile where "
               "simhash IS NOT NULL AND path like '%.js' and "
               "HEX(md5) <> 'd41d8cd98f00b204e9800998ecf8427e' "
               "order by crx_etag, path" +
               (f" LIMIT {int(limit)}" if limit is not None else ""))

    for row in db.fetchall():
        row["simhash"] = int(row["simhash"])
        yield row

def process(bucket, tup):
    crx_etag, trips = tup
    at_least_one_match = 0
    no_matches = 0
    for path, tups in groupby([(x[1], x[2]) for x in trips], key=lambda x: x[0]):
        results = set()
        for _, simhash in tups:
            for ((_, fp_info), _) in bucket.query(int(simhash)):
                results.add(fp_info)
        if len(results) > 0:
            # print("{crx_etag}: {path} - {results}".format(
            #     crx_etag=crx_etag,
            #     path=path,
            #     results=list(results)
            # ))
            at_least_one_match += 1
        else:
            no_matches += 1
    # print(f"{crx_etag}: {at_least_one_match} out of {at_least_one_match + no_matches} files matched")
    return (at_least_one_match, no_matches)


def print_help():
    print("""simhashbucket [OPTION]""")
    print("""  -h, --help                  print this help text""")
    print("""  --limit-cdnjs <N>           only retrieve N rows""")
    print("""  --limit-crxfile <N>         only retrieve N rows""")
    print("""  -t <THREADS>                number of parallel threads""")
    print("""  --read-default-file <PATH>  mysql config file""")
    print("""  --tables <N>                number of tables to use for the bucket (4 or 20 so far)""")

def parse_args(argv):
    limit_cdnjs = None
    limit_crxfile = None
    read_default_file = os.path.expanduser("~/.myslave.cnf")
    parallel = cpu_count()
    tables = 20

    try:
        opts, args = getopt.getopt(argv, "ht:", [
            "limit-cdnjs=", "limit-crxfile=", "read-default-file=", "help", "parallel=", "tables="])
    except getopt.GetoptError:
        print_help()
        sys.exit(2)
    for opt, arg in opts:
        if opt == "--limit-cdnjs":
            limit_cdnjs = int(arg)
        elif opt == "--limit-crxfile":
            limit_crxfile = int(arg)
        elif opt == "--read-default-file":
            read_default_file = arg
        elif opt in ("-t", "--parallel"):
            parallel = int(arg)
        elif opt == "--tables":
            tables = int(arg)

    return limit_cdnjs, limit_crxfile, read_default_file, parallel, tables


def main(args):
    limit_cdnjs, limit_crxfile, read_default_file, parallel, tables = parse_args(args)
    bucket = SimhashBucket(tables)

    with MySQLdb.connect(
            read_default_file=read_default_file,
            compress=True,
            cursorclass=MySQLdb.cursors.SSDictCursor) as db:
        start_build = time.time()
        bucket.addMany([(row["simhash"], row["library"]) for row in get_cdnjs_simhashes(db, limit_cdnjs)])
        print(f"Building the bucket took {format(time.time() - start_build, '.2f')} seconds")

        start_query = time.time()
        total_at_least_one_match = 0
        total_no_matches = 0

        queries = [(row["crx_etag"], row["path"], row["simhash"]) for row in get_crxfile_simhashes(db, limit_crxfile)]
        counter = 0
        with Pool(parallel) as p:
            for at_least_one_match, no_matches in p.imap_unordered(partial(process, bucket),
                                                            [(x, list(y)) for x, y in groupby(queries, key=get_first)],
                                                            100):
                total_at_least_one_match += at_least_one_match
                total_no_matches += no_matches
                counter += 1
                if (counter % 1000 == 0):
                    print(f"{counter} crxfiles finished...")
    print(f"The query took {format(time.time() - start_query, '.2f')} seconds")

    print(f"{total_at_least_one_match} out of {total_no_matches + total_at_least_one_match} "
          f"({format(100.0 * total_at_least_one_match / (total_at_least_one_match + total_no_matches), '.2f')}%) "
          "files matched")


if __name__ == "__main__":
    main(sys.argv[1:])
