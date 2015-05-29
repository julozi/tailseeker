#!/usr/bin/env python3
#
# Copyright (c) 2013-2015 Institute for Basic Science
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# - Hyeshik Chang <hyeshik@snu.ac.kr>
#

import os

def get_topdir():
    if os.path.islink('Snakefile'):
        tailorpkgdir = os.path.dirname(os.readlink('Snakefile'))
        return os.path.abspath(os.path.dirname(tailorpkgdir))
    elif 'TAILOR_DIR' in os.environ:
        return os.environ['TAILOR_DIR']
    else:
        raise ValueError("You need to set an environment variable, TAILOR_DIR.")

TAILOR_DIR = get_topdir()
TARGETS = []

include: os.path.join(TAILOR_DIR, 'tailor', 'snakesupport.py')

from tailor import sequencers

# Variable settings
TILES = sequencers.get_tiles(CONF)

EXP_SAMPLES = sorted(CONF['experimental_samples'].keys())
SPIKEIN_SAMPLES = sorted(CONF['spikein_samples'].keys())
ALL_SAMPLES = sorted(EXP_SAMPLES + SPIKEIN_SAMPLES)

INSERT_READS = sorted(readname for readname in CONF['read_cycles'] if readname[1] != 'i')
INDEX_READS = sorted(readname for readname in CONF['read_cycles'] if readname[1] == 'i')
ALL_READS = INSERT_READS + INDEX_READS

FIRST_CYCLE = min(f for f, l, _ in CONF['read_cycles'].values())
LAST_CYCLE = max(l for f, l, _ in CONF['read_cycles'].values())
NUM_CYCLES = LAST_CYCLE - FIRST_CYCLE + 1

PHIX_ID_REF = ['R5', 6, 40] # identify PhiX reads using 40 bases from the 6th cycle.
CONTAMINANT_ID_REF = ['R5', 1, inf] # use the full length of R5 to identify contamintants

THREADS_MAXIMUM_CORE = CONF['maximum_threads']

BIGALIGNMENTPARTS = 8 # XXX: to be moved somewhere.

# Variable validations
if len(INDEX_READS) != 1:
    raise ValueError("Multi-indexing is not supported yet.")

if FIRST_CYCLE != 1:
    raise ValueError("The pipeline assumes that one of the reads starts from the first cycle.")


subworkflow contaminants_index:
    workdir: os.path.join(TAILOR_DIR, 'seqdb')
    snakefile: os.path.join(TAILOR_DIR, 'tailor', 'contaminantsindex.py')


localrules: all

rule all:
    input: lambda wc: TARGETS

rule basecall_ayb:
    """
    Runs a third-party basecaller, AYB, to call bases. In most cases, the sequence reads
    determined by AYB for 5' end of inserts are more readily aligned to the genome than
    performing that using Illumina RTA.

    Download my modified tree from https://github.com/hyeshik/AYB2. There is few bug fixes
    that were not incorporated into the original version.
    """
    output: temp('scratch/aybcalls/{read}_{tile}.fastq.gz')
    threads: THREADS_MAXIMUM_CORE
    run:
        tileinfo = TILES[wildcards.tile]
        readname = wildcards.read
        first_cycle, last_cycle, read_no = CONF['read_cycles'][readname]
        read_length = last_cycle - first_cycle + 1

        tempdir = make_scratch_dir('aybcalls/{}_{}'.format(readname, tileinfo['id']))
        reads_format = (('' if first_cycle == 1 else 'I{}'.format(first_cycle - 1)) +
                        'R{}'.format(read_length))

        shell('{AYB_BINDIR}/AYB -p {threads} -o {tempdir} -i {tileinfo[datadir]} \
                -b {reads_format} -f fastq.gz -r L{tileinfo[lane]}T{tileinfo[tile]}')
        shell('mv {tempdir}/s_{tileinfo[lane]}_{tileinfo[tile]}.fastq.gz {output}')
        shutil.rmtree(tempdir)


def determine_inputs_demultiplex_signals(wildcards, as_dict=False):
    inputs = {}

    for readid, program in CONF.get('third_party_basecaller', {}).items():
        fastqpath = 'scratch/{program}calls/{readid}_{tile}.fastq.gz'.format(
                        program=program.lower(), readid=readid, tile=wildcards.tile)
        inputs[readid] = fastqpath

    return inputs if as_dict else list(inputs.values())

rule demultiplex_signals:
    """
    Extract signals and basecalls from .CIF, .BCL, and FASTQ files. The sequence and signals
    are sorted into files in the in-house .SQI format after demultiplexing to separate
    reads by their indices (barcodes).
    """
    input: determine_inputs_demultiplex_signals
    output:
        regular = map(temp, expand('scratch/demux-sqi/{sample}_{{tile}}.sqi.gz',
                                   sample=ALL_SAMPLES)),
        unknown = temp('scratch/demux-sqi/Unknown_{tile}.sqi.gz'),
        phixcontrol = temp('scratch/demux-sqi/PhiX_{tile}.sqi.gz')
    threads: min(len(EXP_SAMPLES) + 2, 8)
    run:
        tileinfo = TILES[wildcards.tile]
        index_read_start, index_read_end, read_no = CONF['read_cycles'][INDEX_READS[0]]
        index_read_length = index_read_end - index_read_start + 1

        output_filename = 'scratch/demux-sqi/XX_{tile}.sqi.gz'.format(tile=wildcards.tile)

        phix_cycle_base = CONF['read_cycles'][PHIX_ID_REF[0]][0]
        phix_cycle_start = phix_cycle_base + PHIX_ID_REF[1] - 1
        phix_cycle_length = PHIX_ID_REF[2]

        options = [
            '--data-dir', tileinfo['intensitiesdir'],
            '--run-id', tileinfo['laneid'], '--lane', tileinfo['lane'],
            '--tile', tileinfo['tile'], '--ncycles', NUM_CYCLES,
            '--signal-scale', sequencers.get_signalscale(tileinfo['type']),
            '--barcode-start', index_read_start, '--barcode-length', index_read_length,
            '--writer-command', '{HTSLIB_BINDIR}/bgzip -c > ' + output_filename,
            '--filter-control', 'PhiX,{},{}'.format(phix_cycle_start, phix_cycle_length),
        ]

        altcalls = determine_inputs_demultiplex_signals(wildcards, as_dict=True)
        for readid, inputfile in altcalls.items():
            options.extend([
                '--alternative-call',
                '{},{}'.format(inputfile, CONF['read_cycles'][readid][0])])

        for sample in ALL_SAMPLES:
            options.extend([
                '--sample',
                '{name},{index},{mismatches},{delimiter},{delimpos}'.format(
                    name=sample, index=CONF.get_sample_index(sample),
                    mismatches=CONF['maximum_index_mismatches'][sample],
                    delimiter=CONF['delimiter'][sample][1],
                    delimpos=CONF['delimiter'][sample][0])])

        shell('{BINDIR}/tailseq-retrieve-signals ' +
              ' '.join('"{}"'.format(opt) for opt in options))


rule merge_sqi:
    """
    Merges split sqi.gz files demultiplexed from the original Illumina internal formats
    into one. This will be indexed using tabix for efficient searching and parallel processing.
    Although the official design goal of the BGZF format includes simple concatenations
    of BGZF files, EOF record at the end of the files must be removed during the concatenation.
    """
    input: expand('scratch/demux-sqi/{{sample}}_{tile}.sqi.gz', tile=TILES)
    output: temp('scratch/merged-sqi/{sample}.sqi.gz')
    run:
        input = sorted(input) # to make tabix happy.
        shell('{SCRIPTSDIR}/bgzf-merge.py --output {output} {input}')


rule index_tabix:
    input: '{name}.gz'
    output: '{name}.gz.tbi'
    shell: '{HTSLIB_BINDIR}/tabix -s1 -b2 -e2 -0 {input}'


rule generate_fastq_for_contaminant_filter:
    input: 'scratch/merged-sqi/{sample}.sqi.gz'
    output: temp('confilter/{sample}-con.fastq.gz')
    threads: 2
    run:
        refreadcycles = CONF['read_cycles'][CONTAMINANT_ID_REF[0]]
        refreadlength = refreadcycles[1] - refreadcycles[0] + 1
        idseqend = min(refreadlength, CONTAMINANT_ID_REF[2])
        idseqstart = refreadcycles[0] + CONTAMINANT_ID_REF[1] - 1
        shell('gzip -cd {input} | {BINDIR}/sqi2fq {idseqstart} {idseqend} | \
                gzip -c - > {output}')


def determine_contaminants_index(aligner):
    def _determine_contaminants_index_internal(wildcards):
      try:
        species = CONF['species'][wildcards.sample].replace(' ', '_')
        if aligner == 'gsnap':
            return contaminants_index(
                'contaminants/{species}.gmap/{species}.gmap.genomecomp'.format(species=species))
        elif aligner == 'star':
            return contaminants_index('contaminants/{species}.star/Genome'.format(species=species))
        else:
            raise ValueError('Unknown aligner {}'.format(aligner))
      except:
        import traceback
        traceback.print_exc()
        raise
    return _determine_contaminants_index_internal

if CONF['sequence_aligner'] == 'gsnap':
    rule align_confilter_gsnap:
        input:
            sequence='confilter/{sample}-con.fastq.gz',
            index=determine_contaminants_index('gsnap')
        output: temp('confilter/{sample}-con.bam-{part}')
        threads: THREADS_MAXIMUM_CORE
        run:
            indexdir = os.path.dirname(input.index)
            indexdir, indexname = os.path.split(indexdir)
            shell('{GSNAP_BINDIR}/gsnap -D {indexdir} --gunzip -d {indexname} \
                        -B 4 -O -A sam -m 0.06 -q {wildcards.part}/{BIGALIGNMENTPARTS} \
                        -t {threads} {input.sequence} | \
                   {SAMTOOLS_BINDIR}/samtools view -F 4 -bS - > {output}')

    rule merge_parted_alignments:
        input: expand('{{dir}}/{{sample}}-{{kind}}.bam-{part}', part=range(BIGALIGNMENTPARTS))
        output: '{dir}/{sample}-{kind,[^-]+}.bam'
        threads: THREADS_MAXIMUM_CORE
        run:
            viewcommands = ';'.join('{}/samtools view {}'.format(SAMTOOLS_BINDIR, inpbam)
                                    for inpbam in input)
            scratch_dir = make_scratch_dir('merge_parted_alignments.{}.{}'.format(
                                           wildcards.sample, wildcards.kind))

            shell('({SAMTOOLS_BINDIR}/samtools view -H {input[0]}; ({viewcommands}) | \
                    sort -k1,1 -k2,2n --parallel={threads} -T "{scratch_dir}") | \
                   {SAMTOOLS_BINDIR}/samtools view -@ {threads} -bS - > {output}')
            shutil.rmtree(scratch_dir)

elif CONF['sequence_aligner'] == 'star':
    rule align_confilter_star:
        input:
            sequence='confilter/{sample}-con.fastq.gz',
            index=determine_contaminants_index('star')
        output: 'confilter/{sample}-con.bam' # STAR is too fast to split jobs on-the-fly
        threads: THREADS_MAXIMUM_CORE
        run:
            scratchdir = make_scratch_dir('staralign.' + wildcards.sample)
            indexdir = os.path.dirname(input.index)

            shell("{STAR_BINDIR}/STAR --genomeDir {indexdir} \
                    --readFilesIn {input.sequence} --runThreadN {threads} \
                    --outFilterMultimapNmax 4 --readFilesCommand zcat \
                    --outStd SAM --outFileNamePrefix {scratchdir}/ | \
                   {SAMTOOLS_BINDIR}/samtools view -@ 4 -F 4 -bS -o {output} -")
else:
    raise ValueError('Unknown aligner: {}'.format(ALIGNER))


rule generate_contaminant_list:
    input: 'confilter/{sample}-con.bam'
    output: temp('confilter/{sample}.conlist.gz')
    shell: '{SAMTOOLS_BINDIR}/samtools view {input} | \
            cut -f1 | uniq | sed -e "s,:0*,\t," -e "s/\t$/\t0/" | gzip -c - > {output}'


rule find_duplicated_reads:
    input: sqi='scratch/merged-sqi/{sample}.sqi.gz', \
           sqiindex='scratch/merged-sqi/{sample}.sqi.gz.tbi'
    output: duplist=temp('dupfilter/{sample}.duplist.gz'), \
            dupstats='stats/{sample}.duplicates.csv', \
            dupcounts='dupfilter/{sample}.dupcounts.gz'
    threads: THREADS_MAXIMUM_CORE
    run:
        if wildcards.sample in CONF['dupcheck_regions']:
            checkregions = CONF['dupcheck_regions'][wildcards.sample]
            regionsspec = ' '.join('--region {}:{}'.format(begin, end)
                                   for begin, end in checkregions)
            shell('{SCRIPTSDIR}/find-duplicates.py \
                    --parallel {threads} --output-dupcounts {output.dupcounts} \
                    --output-stats {output.dupstats} {regionsspec} {input.sqi} | \
                    sort -k1,1 -k2,2n | gzip -c - > {output.duplist}')
        else:
            # create null lists to make subsequent rules happy
            import gzip
            gzip.open(output.duplist, 'w')
            open(output.dupstats, 'w')
            gzip.open(output.dupcounts, 'w')


def determine_inputs_for_nondup_id_list(wildcards):
    sample = wildcards.sample
    if sample in EXP_SAMPLES:
        return ['confilter/{}-con.fastq.gz'.format(sample),
                'dupfilter/{}.duplist.gz'.format(sample),
                'confilter/{}.conlist.gz'.format(sample)]
    elif sample in SPIKEIN_SAMPLES:
        return ['scratch/merged-sqi/{}.sqi.gz'.format(sample)]
    else:
        raise ValueError("Unknown sample {}.".format(sample))

rule make_nondup_id_list:
    input: determine_inputs_for_nondup_id_list
    output: temp('confilter/{sample}.lint_ids.gz')
    run:
        if len(input) == 3: # experimental samples
            input = SuffixFilter(input)
            shell('{SCRIPTSDIR}/make-nondup-list.py --fastq {input[fastq.gz]} \
                        --exclude {input[duplist.gz]} --exclude {input[conlist.gz]} | \
                        {HTSLIB_BINDIR}/bgzip -c /dev/stdin > {output}')
        elif len(input) == 1: # spikein samples
            shell('zcat {input} | cut -f1,2 | {HTSLIB_BINDIR}/bgzip -c /dev/stdin > {output}')
        else:
            raise ValueError("make_nondup_id_list: programming error")


rule generate_lint_sqi:
    input:
        sqi='scratch/merged-sqi/{sample}.sqi.gz',
        sqi_index='scratch/merged-sqi/{sample}.sqi.gz.tbi',
        whitelist='confilter/{sample}.lint_ids.gz',
        whitelist_index='confilter/{sample}.lint_ids.gz.tbi'
    output: 'sequences/{sample}.sqi.gz'
    threads: THREADS_MAXIMUM_CORE
    run:
        sample = wildcards.sample
        preambleopt = balanceopt = ''

        if sample in CONF['preamble_sequence']:
            preambleopt = ('--preamble-sequence {} --preamble-position {} '
                           '--preamble-mismatch 1').format(CONF['preamble_sequence'][sample],
                                                           CONF['read_cycles']['R3'][0])
        elif sample in CONF['preamble_size']:
            preambleend = CONF['read_cycles']['R3'][0] - 1 + CONF['preamble_size'][sample]
            preambleopt = '--preamble-end {}'.format(preambleend)

        if sample in CONF['balance_check']:
            balanceopt = ('--balance-region {}:{} --balance-minimum {}').format(*
                            CONF['balance_check'][sample])

        # This script uses three threads per a parallel job. Process jobs as many as half of
        # the allowed threads which is optimal as there are some bottlenecks in the first two
        # processes in each pipe.
        paralleljobs = max(1, threads // 2)
        shell('{SCRIPTSDIR}/lint-sequences-sqi.py --id-list {input.whitelist} \
                --output {output} \
                --parallel {paralleljobs} {preambleopt} {balanceopt} {input.sqi}')


rule collect_color_matrices:
    output: 'signalproc/colormatrix.pickle'
    run:
        import base64, pickle

        matrix_files = {}
        for vtile, tileinfo in TILES.items():
            matrix_dir = os.path.join(tileinfo['intensitiesdir'], 'BaseCalls', 'Matrix')
            matrix_filename = 's_{tileinfo[lane]}_READNO_{tileinfo[tile]}_matrix.txt'.format(
                                tileinfo=tileinfo)
            matrix_fn_pattern = os.path.join(matrix_dir, matrix_filename)
            matrix_files[vtile] = matrix_fn_pattern

        tilemapping = base64.encodebytes(pickle.dumps(matrix_files, 0)).decode('ascii')
        shell('{SCRIPTSDIR}/collect-color-matrices.py \
                    --tile-mapping \'{tilemapping}\' --output {output}')


rule calculate_phix_signal_scaling_factor:
    input:
        phix='scratch/merged-sqi/PhiX.sqi.gz',
        phix_index='scratch/merged-sqi/PhiX.sqi.gz.tbi',
        colormatrix='signalproc/colormatrix.pickle'
    output:
        paramout='signalproc/signal-scaling.phix-ref.pickle',
        statsout='stats/signal-scaling-basis.csv'
    threads: THREADS_MAXIMUM_CORE
    resources: high_end_cpu=1
    run:
        readstart, readend, readno = CONF['read_cycles']['R3']
        shell('{SCRIPTSDIR}/prepare-phix-signal-scaler.py --parallel {threads} \
                --output {output.paramout} --read {readno} \
                --read-range {readstart}:{readend} \
                --color-matrix {input.colormatrix} \
                --sample-number-stats {output.statsout} {input.phix}')


rule prepare_signal_stabilizer:
    input:
        colormatrix='signalproc/colormatrix.pickle',
        signals='sequences/{sample}.sqi.gz',
        signals_index='sequences/{sample}.sqi.gz.tbi',
        cyclescaling='signalproc/signal-scaling.phix-ref.pickle'
    output: 'signalproc/signal-scaling-{sample}.stabilizer.pickle'
    threads: THREADS_MAXIMUM_CORE
    run:
        preamble_size = CONF['preamble_size'][wildcards.sample]
        high_probe_range = '{}:{}'.format(
                preamble_size + SIGNAL_STABILIZER_POLYA_DETECTION_RANGE[0],
                preamble_size + SIGNAL_STABILIZER_POLYA_DETECTION_RANGE[1])
        high_probe_scale_inspection = '{}:{}'.format(
                preamble_size + SIGNAL_STABILIZER_TARGET_RANGE[0],
                preamble_size + SIGNAL_STABILIZER_TARGET_RANGE[1])
        high_probe_scale_basis = '{}:{}'.format(
                preamble_size + SIGNAL_STABILIZER_REFERENCE_RANGE[0],
                preamble_size + SIGNAL_STABILIZER_REFERENCE_RANGE[1])

        cyclestart, cycleend, readno = CONF['read_cycles']['R3']
        shell('{SCRIPTSDIR}/prepare-signal-stabilizer.py \
                --parallel {threads} --output {output} \
                --read {readno} --color-matrix {input.colormatrix} \
                --cycle-scaling {input.cyclescaling} \
                --high-probe-range {high_probe_range} \
                --high-probe-scale-inspection {high_probe_scale_inspection} \
                --high-probe-scale-basis {high_probe_scale_basis} \
                --read-range {cyclestart}:{cycleend} --spot-norm-length {preamble_size} \
                {input.signals}')


def determine_inputs_calc_pasignals_v2(wildcards):
    sample = wildcards.sample
    stabilizer_ref = sample if sample in EXP_SAMPLES else CONF['spikein_scaling_ref']

    return ['signalproc/colormatrix.pickle',
            'sequences/{sample}.sqi.gz'.format(sample=sample),
            'sequences/{sample}.sqi.gz.tbi'.format(sample=sample),
            'signalproc/signal-scaling.phix-ref.pickle',
            'signalproc/signal-scaling-{sample}.stabilizer.pickle'.format(
                sample=stabilizer_ref)]

rule calculate_pasignals_v2:
    input: determine_inputs_calc_pasignals_v2
    output: 'scores/{sample}.pa2score.gz'
    threads: THREADS_MAXIMUM_CORE
    run:
        input = SuffixFilter(input)
        shell('{SCRIPTSDIR}/calculate-pasignals.py --parallel {threads} \
                --scaling-params {input[stabilizer.pickle]} {input[sqi.gz]} \
                > {output}')


rule pick_spikein_samples_for_training:
    input: 'scores/{sample}.pa2score.gz'
    output:
        result='learning/{sample}.trainer.npy',
        qcplot='qcplots/{sample}.trainer.pdf',
        idlist='learning/{sample}.trainer.idlist'
    run:
        trim_len = CONF['spikein_training_length'][wildcards.sample]
        samples_to_learn = CONF['spikein_learning_num_samples']

        shell('{SCRIPTSDIR}/pick-samples-to-learn.py --input-pascore {input} \
                --output {output.result} --output-qc-plot {output.qcplot} \
                --qc-plot-range 0:2 \
                --pass1 {samples_to_learn[pass1]} \
                --pass2 {samples_to_learn[pass2]} \
                --support-fraction 0.75 --contamination 0.4 \
                --granule-size 15 --trim {trim_len} \
                --output-training-set-list {output.idlist}')


rule learn_pascores_from_spikeins:
    input: expand('learning/{sample}.trainer.npy', sample=CONF['spikeins_to_learn'])
    output: 'learning/model.pickle'
    shell: '{SCRIPTSDIR}/learn-spikein-pa-score.py \
                --preset v2 \
                --clip-minimum {PASIGNAL_CLIP_MIN} --clip-maximum {PASIGNAL_CLIP_MAX} \
                --output {output} {input}'


TARGETS.extend(expand('polya/{sample}.polya-calls.gz', sample=ALL_SAMPLES))
rule measure_polya:
    input:
        sqi='sequences/{sample}.sqi.gz',
        sqiindex='sequences/{sample}.sqi.gz.tbi',
        score='scores/{sample}.pa2score.gz',
        scoreinex='scores/{sample}.pa2score.gz.tbi',
        model='learning/model.pickle'
    output: 'polya/{sample}.polya-calls.gz'
    threads: THREADS_MAXIMUM_CORE
    shell: '{SCRIPTSDIR}/measure-polya-tails.py \
                --input-sqi {input.sqi} --input-pa {input.score} \
                --model {input.model} --parallel {threads} --output {output}'


TARGETS.extend(expand('fastq/{sample}_{readno}.fastq.gz',
                      sample=EXP_SAMPLES, readno=INSERT_READS))
rule generate_fastq:
    input:
        sqi='sequences/{sample}.sqi.gz',
        sqiindex='sequences/{sample}.sqi.gz.tbi',
        pacall='polya/{sample}.polya-calls.gz',
        pacallindex='polya/{sample}.polya-calls.gz.tbi'
    output: expand('fastq/{{sample}}_{readno}.fastq.gz', readno=INSERT_READS)
    params: output='fastq/{sample}_XX.fastq.gz'
    threads: THREADS_MAXIMUM_CORE
    run:
        reads = ''
        for readname in INSERT_READS:
            start, end, _ = CONF['read_cycles'][readname]
            if wildcards.sample not in CONF['delimiter']:
                trim = 'N'
            elif start <= CONF['delimiter'][wildcards.sample][0] <= end:
                delimend = (CONF['delimiter'][wildcards.sample][0] +
                            len(CONF['delimiter'][wildcards.sample][1]))
                trim = end - (delimend + MAXIMUM_DELIMITER_MISALIGNMENT - 1)
            else:
                trim = 'N'

            reads += ' {},{},{},{}'.format(readname, start, end, trim)

        shell('{SCRIPTSDIR}/generate-fastq.py \
                --input-sqi {input.sqi} --input-pa-call {input.pacall} \
                --parallel {threads} --output {params.output} {reads}')

# ex: syntax=snakemake