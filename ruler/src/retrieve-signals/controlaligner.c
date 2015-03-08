/*
 * controlaligner.c
 *
 * Copyright (c) 2015 Hyeshik Chang
 * 
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 * 
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 * 
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 * - Hyeshik Chang <hyeshik@snu.ac.kr>
 */

#define _BSD_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <getopt.h>
#include <errno.h>
#include <string.h>
#include <stdint.h>
#include <limits.h>
#include <endian.h>
#include <zlib.h>
#include "tailseq-retrieve-signals.h"
#include "ssw.h"


#define CONTROL_SEQUENCE_SPACING            20 /* space between forward and reverse strands */


static const int8_t DNABASE2NUM[128] = { 
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 0, 4, 1, 4, 4, 4, 2, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 0, 4, 1, 4, 4, 4, 2, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4
};


void
initialize_ssw_score_matrix(int8_t *score_mat, int8_t match_score, int8_t mismatch_score)
{
    int l, m, k;

    /* initialize score matrix for Smith-Waterman alignment */
    for (l = k = 0; l < 4; l++) {
        for (m = 0; m < 4; m++)
            score_mat[k++] = (l == m) ? match_score : -mismatch_score;
        score_mat[k++] = 0; /* no penalty for ambiguous base */
    }

    for (m = 0; m < 5; m++)
        score_mat[k++] = 0;
}


size_t
load_control_sequence(int8_t **control_seq)
{
    int8_t *ctlseq, *pctlseq;
    ssize_t len, i;
    static const int8_t reverse_base[]={3, 2, 1, 0, 4};

    len = strlen(phix_control_sequence);
    ctlseq = malloc(len * 2 + CONTROL_SEQUENCE_SPACING);
    if (ctlseq == NULL) {
        perror("load_control_sequence");
        return -1;
    }

    /* forward strand */
    for (i = 0, pctlseq = ctlseq; i < len; i++)
        *pctlseq++ = DNABASE2NUM[(int)phix_control_sequence[i]];

    for (i = 0, pctlseq = ctlseq + len; i < CONTROL_SEQUENCE_SPACING; i++)
        *pctlseq++ = 4;

    /* reverse strand */
    for (i = 0, pctlseq = ctlseq + len * 2 + CONTROL_SEQUENCE_SPACING - 1; i < len; i++)
        *pctlseq-- = reverse_base[(int)DNABASE2NUM[(int)phix_control_sequence[i]]];

    *control_seq = ctlseq;

    return len * 2 + CONTROL_SEQUENCE_SPACING;
}


int
try_alignment_to_control(const char *sequence_read, const int8_t *control_seq,
                         ssize_t control_seq_length,
                         struct ControlFilterInfo *control_info,
                         int8_t *ssw_score_mat, int32_t min_control_alignment_score,
                         int32_t control_alignment_mask_len)
{
    s_profile *alnprof;
    s_align *alnresult;
    int8_t read_seq[control_info->read_length];
    size_t i, j;
    int r;

    for (i = 0, j = control_info->first_cycle; i < control_info->read_length; i++, j++)
        read_seq[i] = DNABASE2NUM[(int)sequence_read[j]];

    alnprof = ssw_init(read_seq, control_info->read_length, ssw_score_mat, 5, 0);
    if (alnprof == NULL) {
        perror("try_alignment_to_control");
        return -1;
    }

    alnresult = ssw_align(alnprof, control_seq, control_seq_length,
                          CONTROL_ALIGN_GAP_OPEN_SCORE,
                          CONTROL_ALIGN_GAP_EXTENSION_SCORE, 2,
                          min_control_alignment_score,
                          0, control_alignment_mask_len);
    r = (alnresult != NULL && alnresult->score1 >= min_control_alignment_score);

    if (alnresult != NULL)
        align_destroy(alnresult);

    init_destroy(alnprof);

    return r;
}
