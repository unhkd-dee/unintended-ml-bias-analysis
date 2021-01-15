# Lint as: python3
"""Analysis of model bias.

We look at differences in model scores as a way to compare bias in different
models.

The functions in this file expect scored data in a data frame with columns:

<text_col>: Column containing text of the example. This column name is
    passed in as a parameter of any function that needs access to it.
<label_col>: Column containing a boolean representing the example's
    true label.
<model name>: One column per model, each containing the model's predicted
    score for this example.
<subgroup>: One column per subgroup to evaluate bias for. These columns
    may be generated by add_subgroup_columns_from_text (when being "in"
    a subgroup means the text contains a certain term), or may be
    additional label columns from the original test data.
"""

from __future__ import division

import base64
import io
import os
import re

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy.stats as stats
import seaborn as sns
from sklearn import metrics


PINNED_AUC = 'pinned_auc'  # Deprecated, don't use pinned AUC anymore!
SUBGROUP_AUC = 'subgroup_auc'
NEGATIVE_CROSS_AUC = 'bpsn_auc'
POSITIVE_CROSS_AUC = 'bnsp_auc'
NEGATIVE_AEG = 'negative_aeg'
POSITIVE_AEG = 'positive_aeg'
NEGATIVE_ASEG = 'negative_aseg'
POSITIVE_ASEG = 'positive_aseg'

SUBSET_SIZE = 'subset_size'
SUBGROUP = 'subgroup'

METRICS = [
    SUBGROUP_AUC, NEGATIVE_CROSS_AUC, POSITIVE_CROSS_AUC, NEGATIVE_AEG,
    POSITIVE_AEG
]
AUCS = [SUBGROUP_AUC, NEGATIVE_CROSS_AUC, POSITIVE_CROSS_AUC]
AEGS = [NEGATIVE_AEG, POSITIVE_AEG]
ASEGS = [NEGATIVE_ASEG, POSITIVE_ASEG]


def column_name(model, metric):
  return model + '_' + metric


def compute_auc(y_true, y_pred):
  try:
    return metrics.roc_auc_score(y_true, y_pred)
  except ValueError:
    return np.nan


### Per-subgroup pinned AUC analysis.
def model_family_auc(dataset, model_names, label_col):
  aucs = [
      compute_auc(dataset[label_col], dataset[model_name])
      for model_name in model_names
  ]
  return {
      'aucs': aucs,
      'mean': np.mean(aucs),
      'median': np.median(aucs),
      'std': np.std(aucs),
  }


def plot_model_family_auc(dataset, model_names, label_col, min_auc=0.9):
  result = model_family_auc(dataset, model_names, label_col)
  print('mean AUC:', result['mean'])
  print('median:', result['median'])
  print('stddev:', result['std'])
  plt.hist(result['aucs'])
  plt.gca().set_xlim([min_auc, 1.0])
  plt.show()
  return result


def read_identity_terms(identity_terms_path):
  with open(identity_terms_path) as f:
    return [term.strip() for term in f.readlines()]


def add_subgroup_columns_from_text(df, text_column, subgroups,
                                   expect_spaces_around_words=True):
  """Adds a boolean column for each subgroup to the data frame.

  New column contains True if the text contains that subgroup term.

  Args:
    df: Pandas dataframe to process.
    text_column: Column in df containing the text.
    subgroups: List of subgroups to search text_column for.
    expect_spaces_around_words: Whether to expect subgroup to be surrounded by
      spaces in the text_column.  Set to False to for languages which do not
      use spaces.
  """
  for term in subgroups:
    if expect_spaces_around_words:
      # pylint: disable=cell-var-from-loop
      df[term] = df[text_column].apply(
          lambda x: bool(re.search('\\b' + term + '\\b', x,
                                   flags=re.UNICODE | re.IGNORECASE)))
    else:
      df[term] = df[text_column].str.contains(term, case=False)


def balanced_subgroup_subset(df, subgroup):
  """Returns data subset containing subgroup balanced with sample of other data.

  We draw a random sample from the dataset of other examples because we don't
  care about the model's ability to distinguish toxic from non-toxic just
  within the subgroup-specific dataset, but rather its ability to distinguish
  for the subgroup-specific subset within the context of a larger distribution
  of data.

  Args:
    df: Pandas dataframe to process.
    subgroup: subgroup from which to balance data with other samples.

  Note: Uses a fixed random seed for reproducability.
  """
  subgroup_df = df[df[subgroup]]
  nonsubgroup_df = df[~df[subgroup]].sample(len(subgroup_df), random_state=25)
  combined = pd.concat([subgroup_df, nonsubgroup_df])
  return combined


def model_family_name(model_names):
  """Given a list of model names, returns the common prefix."""
  prefix = os.path.commonprefix(model_names)
  if not prefix:
    raise ValueError("couldn't determine family name from model names")
  return prefix.strip('_')


def normalized_mwu(data1, data2, model_name):
  """Calculate number of datapoints with a higher score in data1 than data2."""
  scores_1 = data1[model_name]
  scores_2 = data2[model_name]
  n1 = len(scores_1)
  n2 = len(scores_2)
  if n1 == 0 or n2 == 0:
    return None
  u, _ = stats.mannwhitneyu(scores_1, scores_2, alternative='less')
  return u / (n1 * n2)


def compute_average_squared_equality_gap(df, subgroup, label, model_name):
  """Returns the positive and negative ASEG metrics."""
  subgroup_df = df[df[subgroup]]
  background_df = df[~df[subgroup]]
  if subgroup_df.empty or background_df.empty:
    return None, None
  thresholds = np.linspace(1.0, 0.0, num=1000)
  s_fpr, s_tpr = positive_rates(subgroup_df, model_name, label, thresholds)
  b_fpr, b_tpr = positive_rates(background_df, model_name, label, thresholds)
  if s_fpr and s_tpr and b_fpr and b_tpr:
    return squared_diff_integral(s_tpr, b_tpr), squared_diff_integral(
        s_fpr, b_fpr)
  return None, None


def squared_diff_integral(y, x):
  return np.trapz(np.square(np.subtract(y, x)), x)


def compute_negative_aeg(df, subgroup, label, model_name):
  mwu = normalized_mwu(df[~df[subgroup] & ~df[label]],
                       df[df[subgroup] & ~df[label]], model_name)
  if mwu is None:
    return None
  return 0.5 - mwu


def compute_positive_aeg(df, subgroup, label, model_name):
  mwu = normalized_mwu(df[~df[subgroup] & df[label]],
                       df[df[subgroup] & df[label]], model_name)
  if mwu is None:
    return None
  return 0.5 - mwu


def compute_subgroup_auc(df, subgroup, label, model_name):
  subgroup_examples = df[df[subgroup]]
  return compute_auc(subgroup_examples[label], subgroup_examples[model_name])


def compute_negative_cross_auc(df, subgroup, label, model_name):
  """Computes the AUC of the within-subgroup negative examples and the background positive examples."""
  subgroup_negative_examples = df[df[subgroup] & ~df[label]]
  non_subgroup_positive_examples = df[~df[subgroup] & df[label]]
  examples = subgroup_negative_examples.append(non_subgroup_positive_examples)
  return compute_auc(examples[label], examples[model_name])


def compute_positive_cross_auc(df, subgroup, label, model_name):
  """Computes the AUC of the within-subgroup positive examples and the background negative examples."""
  subgroup_positive_examples = df[df[subgroup] & df[label]]
  non_subgroup_negative_examples = df[~df[subgroup] & ~df[label]]
  examples = subgroup_positive_examples.append(non_subgroup_negative_examples)
  return compute_auc(examples[label], examples[model_name])


def compute_bias_metrics_for_subgroup_and_model(dataset,
                                                subgroup,
                                                model,
                                                label_col,
                                                include_asegs=False):
  """Computes per-subgroup metrics for one model and subgroup."""
  record = {
      SUBGROUP: subgroup,
      SUBSET_SIZE: len(dataset[dataset[subgroup]])
  }
  record[column_name(model, SUBGROUP_AUC)] = compute_subgroup_auc(
      dataset, subgroup, label_col, model)
  record[column_name(model, NEGATIVE_CROSS_AUC)] = compute_negative_cross_auc(
      dataset, subgroup, label_col, model)
  record[column_name(model, POSITIVE_CROSS_AUC)] = compute_positive_cross_auc(
      dataset, subgroup, label_col, model)
  record[column_name(model, NEGATIVE_AEG)] = compute_negative_aeg(
      dataset, subgroup, label_col, model)
  record[column_name(model, POSITIVE_AEG)] = compute_positive_aeg(
      dataset, subgroup, label_col, model)

  if include_asegs:
    record[column_name(model, POSITIVE_ASEG)], record[column_name(
        model, NEGATIVE_ASEG)] = compute_average_squared_equality_gap(
            dataset, subgroup, label_col, model)
  return record


def compute_bias_metrics_for_model(dataset,
                                   subgroups,
                                   model,
                                   label_col,
                                   include_asegs=False):
  """Computes per-subgroup metrics for all subgroups and one model."""
  records = []
  for subgroup in subgroups:
    subgroup_record = compute_bias_metrics_for_subgroup_and_model(
        dataset, subgroup, model, label_col, include_asegs)
    records.append(subgroup_record)
  return pd.DataFrame(records)


def compute_bias_metrics_for_models(dataset,
                                    subgroups,
                                    models,
                                    label_col,
                                    include_asegs=False):
  """Computes per-subgroup metrics for all subgroups and a list of models."""
  output = None

  for model in models:
    model_results = compute_bias_metrics_for_model(dataset, subgroups, model,
                                                   label_col, include_asegs)
    if output is None:
      output = model_results
    else:
      output = output.merge(model_results, on=[SUBGROUP, SUBSET_SIZE])
  return output


def merge_family(model_family_results, models, metrics_list):
  output = model_family_results.copy()
  for metric in metrics_list:
    metric_columns = [column_name(model, metric) for model in models]
    output[column_name(model_family_name(models),
                       metric)] = output[metric_columns].values.tolist()
    output = output.drop(metric_columns, axis=1)
  return output


def compute_bias_metrics_for_model_families(dataset,
                                            subgroups,
                                            model_families,
                                            label_col,
                                            include_asegs=False):
  """Computes per-subgroup metrics for all subgroups and a list of model families (list of lists of models)."""
  output = None
  metrics_list = METRICS
  if include_asegs:
    metrics_list = METRICS + ASEGS
  for model_family in model_families:
    model_family_results = compute_bias_metrics_for_models(
        dataset, subgroups, model_family, label_col, include_asegs)
    model_family_results = merge_family(model_family_results, model_family,
                                        metrics_list)
    if output is None:
      output = model_family_results
    else:
      output = output.merge(
          model_family_results, on=[SUBGROUP, SUBSET_SIZE])
  return output


# TODO(lucyvasserman): Deprecate this, and Pinned AUC completely.
def per_subgroup_aucs(dataset, subgroups, model_families, label_col,
                      include_asegs=False):
  """Computes per-subgroup metrics for all subgroups and model families.

  Includes deprecated pinned AUC.
  """
  new_bias_metrics = compute_bias_metrics_for_model_families(
      dataset, subgroups, model_families, label_col,
      include_asegs=include_asegs)

  records = []
  for subgroup in subgroups:
    subgroup_subset = balanced_subgroup_subset(dataset, subgroup)
    subgroup_record = {
        SUBGROUP: subgroup,
        'pinned_auc_subset_size': len(subgroup_subset)
    }
    for model_family in model_families:
      family_name = model_family_name(model_family)
      aucs = [
          compute_auc(subgroup_subset[label_col], subgroup_subset[model_name])
          for model_name in model_family
      ]
      subgroup_record.update({
          family_name + '_mean': np.mean(aucs),
          family_name + '_median': np.median(aucs),
          family_name + '_std': np.std(aucs),
          family_name + '_aucs': aucs,
      })
    records.append(subgroup_record)
  pinned_auc_results = pd.DataFrame(records)
  return new_bias_metrics.merge(pinned_auc_results, on=[SUBGROUP])


### Equality of opportunity negative rates analysis.


def confusion_matrix_counts(df, score_col, label_col, threshold):
  return {
      'tp': len(df[(df[score_col] >= threshold) & df[label_col]]),
      'tn': len(df[(df[score_col] < threshold) & ~df[label_col]]),
      'fp': len(df[(df[score_col] >= threshold) & ~df[label_col]]),
      'fn': len(df[(df[score_col] < threshold) & df[label_col]]),
  }


def positive_rates(df, score_col, label_col, thresholds):
  """Compute false positive and true positive rates."""
  tpr = []
  fpr = []
  for threshold in thresholds:
    confusion = confusion_matrix_counts(df, score_col, label_col, threshold)
    if (confusion['tp'] + confusion['fn'] == 0 or
        confusion['fp'] + confusion['tn'] == 0):
      return None, None
    tpr.append(confusion['tp'] / (confusion['tp'] + confusion['fn']))
    fpr.append(confusion['fp'] / (confusion['fp'] + confusion['tn']))
  return fpr, tpr


# https://en.wikipedia.org/wiki/Confusion_matrix
def compute_confusion_rates(df, score_col, label_col, threshold):
  """Compute confusion rates."""
  confusion = confusion_matrix_counts(df, score_col, label_col, threshold)
  actual_positives = confusion['tp'] + confusion['fn']
  actual_negatives = confusion['tn'] + confusion['fp']
  # True positive rate, sensitivity, recall.
  tpr = confusion['tp'] / actual_positives
  # True negative rate, specificity.
  tnr = confusion['tn'] / actual_negatives
  # False positive rate, fall-out.
  fpr = 1 - tnr
  # False negative rate, miss rate.
  fnr = 1 - tpr
  # Precision, positive predictive value.
  precision = confusion['tp'] / (confusion['tp'] + confusion['fp'])
  return {
      'tpr': tpr,
      'tnr': tnr,
      'fpr': fpr,
      'fnr': fnr,
      'precision': precision,
      'recall': tpr,
  }


def compute_equal_error_rate(df, score_col, label_col, num_thresholds=101):
  """Find threshold where false negative and false positive counts are equal."""
  # Note: I'm not sure if this should be based on the false positive/negative
  # *counts*, or the *rates*. However, they should be equivalent for balanced
  # datasets.
  thresholds = np.linspace(0, 1, num_thresholds)
  min_threshold = None
  min_confusion_matrix = None
  min_diff = float('inf')
  for threshold in thresholds:
    confusion_matrix = confusion_matrix_counts(df, score_col, label_col,
                                               threshold)
    difference = abs(confusion_matrix['fn'] - confusion_matrix['fp'])
    if difference <= min_diff:
      min_diff = difference
      min_confusion_matrix = confusion_matrix
      min_threshold = threshold
    else:
      # min_diff should be monotonically non-decreasing, so once it
      # increases we can break. Yes, we could do a binary search instead.
      break
  return {
      'threshold': min_threshold,
      'confusion_matrix': min_confusion_matrix,
  }


def per_model_eer(dataset, label_col, model_names, num_eer_thresholds=101):
  """Computes the equal error rate for every model on the given dataset."""
  model_name_to_eer = {}
  for model_name in model_names:
    eer = compute_equal_error_rate(dataset, model_name, label_col,
                                   num_eer_thresholds)
    model_name_to_eer[model_name] = eer['threshold']
  return model_name_to_eer


def per_subgroup_negative_rates(df, subgroups, model_families, threshold,
                                label_col):
  """Computes per-subgroup true/false negative rates for all model families.

  Args:
    df: dataset to compute rates on.
    subgroups: negative rates are computed on subsets of the dataset
      containing each subgroup.
    model_families: list of model families; each model family is a list of
      model names in the family.
    threshold: threshold to use to compute negative rates. Can either be a
      float, or a dictionary mapping model name to float threshold in order to
      use a different threshold for each model.
    label_col: column in df containing the boolean label.

  Returns:
    DataFrame with per-subgroup false/true negative rates for each model
    family. Results are summarized across each model family, giving mean,
    median, and standard deviation of each negative rate.
  """
  records = []
  for subgroup in subgroups:
    if subgroup is None:
      subgroup_subset = df
    else:
      subgroup_subset = df[df[subgroup]]
    subgroup_record = {
        SUBGROUP: subgroup,
        SUBSET_SIZE: len(subgroup_subset)
    }
    for model_family in model_families:
      family_name = model_family_name(model_family)
      family_rates = []
      for model_name in model_family:
        model_threshold = (
            threshold[model_name] if isinstance(threshold, dict) else threshold)
        assert isinstance(model_threshold, float)
        model_rates = compute_confusion_rates(subgroup_subset, model_name,
                                              label_col, model_threshold)
        family_rates.append(model_rates)
      tnrs, fnrs = ([rates['tnr'] for rates in family_rates],
                    [rates['fnr'] for rates in family_rates])
      subgroup_record.update({
          family_name + '_tnr_median': np.median(tnrs),
          family_name + '_tnr_mean': np.mean(tnrs),
          family_name + '_tnr_std': np.std(tnrs),
          family_name + '_tnr_values': tnrs,
          family_name + '_fnr_median': np.median(fnrs),
          family_name + '_fnr_mean': np.mean(fnrs),
          family_name + '_fnr_std': np.std(fnrs),
          family_name + '_fnr_values': fnrs,
      })
    records.append(subgroup_record)
  return pd.DataFrame(records)


### Summary metrics
def diff_per_subgroup_from_overall(overall_metrics, per_subgroup_metrics,
                                   model_families, metric_column,
                                   squared_error):
  """Compute sum of differences between per-subgroup and overall values.

  Summed over all subgroups and models, i.e.
    sum(|overall_i - per-subgroup_i,t|) for i in models and t in subgroups.

  Args:
    overall_metrics: dict of model familiy to list of score values for the
      overall dataset (one per model instance).
    per_subgroup_metrics: DataFrame of scored results, one subgroup per row.
      Expected to have a column named model family name + metric column, which
      contains a list of one score per model instance.
    model_families: list of model families; each model family is a list of
      model names in the family.
    metric_column: column name suffix in the per_subgroup_metrics df where the
      per-subgroup data to be diffed is stored.
    squared_error: boolean indicating whether to use squared error or just
      absolute difference.

  Returns:
    A dictionary of model family name to sum of differences value for that
    model family.
  """

  def calculate_error(overall_score, per_group_score):
    diff = overall_score - per_group_score
    return diff**2 if squared_error else abs(diff)

  diffs = {}
  for fams in model_families:
    family_name = model_family_name(fams)
    family_overall_metrics = overall_metrics[family_name]
    diffs[family_name] = 0.0
    # Loop over the subgroups. one_subgroup_metric_list is a list of the
    # per-subgroup values, one per model instance.
    for one_subgroup_metric_list in per_subgroup_metrics[family_name +
                                                         metric_column]:
      # Zips the overall scores with the per-subgroup scores, pairing results
      # from the same model instance, then diffs those pairs and sums.
      per_subgroup_metric_diffs = [
          calculate_error(overall_score, per_subgroup_score)
          for overall_score, per_subgroup_score in zip(
              family_overall_metrics, one_subgroup_metric_list)
      ]
      diffs[family_name] += sum(per_subgroup_metric_diffs)
  return diffs


def per_subgroup_auc_diff_from_overall(dataset,
                                       subgroups,
                                       model_families,
                                       squared_error,
                                       normed_auc=False):
  """Calculates the sum of differences between the per-subgroup pinned AUC and the overall AUC."""
  per_subgroup_auc_results = per_subgroup_aucs(dataset, subgroups,
                                               model_families, 'label')
  overall_aucs = {}
  for fams in model_families:
    family_name = model_family_name(fams)
    overall_aucs[family_name] = model_family_auc(dataset, fams, 'label')['aucs']
  auc_column = '_normalized_pinned_aucs' if normed_auc else '_aucs'
  d = diff_per_subgroup_from_overall(overall_aucs, per_subgroup_auc_results,
                                     model_families, auc_column, squared_error)
  return pd.DataFrame(
      list(d.items()),
      columns=['model_family', 'pinned_auc_equality_difference'])


def per_subgroup_nr_diff_from_overall(df, subgroups, model_families, threshold,
                                      metric_column, squared_error):
  """Calculates the sum of differences between the per-subgroup true or false negative rate and the overall rate."""
  per_subgroup_nrs = per_subgroup_negative_rates(df, subgroups, model_families,
                                                 threshold, 'label')
  all_nrs = per_subgroup_negative_rates(df, [None], model_families, threshold,
                                        'label')
  overall_nrs = {}
  for fams in model_families:
    family_name = model_family_name(fams)
    overall_nrs[family_name] = all_nrs[family_name + metric_column][0]
  return diff_per_subgroup_from_overall(overall_nrs, per_subgroup_nrs,
                                        model_families, metric_column,
                                        squared_error)


def per_subgroup_fnr_diff_from_overall(df, subgroups, model_families, threshold,
                                       squared_error):
  """Calculates the sum of differences between the per-subgroup false negative rate and the overall FNR."""
  d = per_subgroup_nr_diff_from_overall(df, subgroups, model_families,
                                        threshold, '_fnr_values', squared_error)
  return pd.DataFrame(
      list(d.items()), columns=['model_family', 'fnr_equality_difference'])


def per_subgroup_tnr_diff_from_overall(df, subgroups, model_families, threshold,
                                       squared_error):
  """Calculates the sum of differences between the per-subgroup true negative rate and the overall TNR."""
  d = per_subgroup_nr_diff_from_overall(df, subgroups, model_families,
                                        threshold, '_tnr_values', squared_error)
  return pd.DataFrame(
      list(d.items()), columns=['model_family', 'tnr_equality_difference'])


### Plotting.


def per_subgroup_scatterplots(df,
                              subgroup_col,
                              values_col,
                              title='',
                              y_lim=(0.8, 1.0),
                              figsize=(15, 5),
                              point_size=8,
                              file_name='plot'):
  """Displays a series of one-dimensional scatterplots, 1 scatterplot per subgroup.

  Args:
    df: DataFrame contain subgroup_col and values_col.
    subgroup_col: Column containing subgroups.
    values_col: Column containing collection of values to plot (each cell
      should contain a sequence of values, e.g. the AUCs for multiple models
      from the same family).
    title: Plot title.
    y_lim: Plot bounds for y axis.
    figsize: Plot figure size.
  """
  fig = plt.figure(figsize=figsize)
  ax = fig.add_subplot(111)
  for i, (_, row) in enumerate(df.iterrows()):
    # For each subgroup, we plot a 1D scatterplot. The x-value is the position
    # of the item in the dataframe. To change the ordering of the subgroups,
    # sort the dataframe before passing to this function.
    x = [i] * len(row[values_col])
    y = row[values_col]
    ax.scatter(x, y, s=point_size)
  ax.set_xticklabels(df[subgroup_col], rotation=90)
  ax.set_xticks(list(range(len(df))))
  ax.set_ylim(y_lim)
  ax.set_title(title)
  fig.tight_layout()
  fig.savefig('/tmp/%s_%s.eps' % (file_name, values_col), format='eps')


def save_inline_png(fig, out, **kwargs):
  """Saves figure as an inline data URI resource."""
  if isinstance(out, str):
    fig.savefig(out, format='png', **kwargs)
    return
  s = io.BytesIO()
  fig.savefig(s, format='png', **kwargs)
  out.write('<img src="data:image/png;base64,{}"/>'.format(
      base64.b64encode(s.getvalue()).decode('ascii')))


def plot_metric_heatmap(bias_metrics_results,
                        models,
                        metrics_list,
                        out=None,
                        cmap=None,
                        show_subgroups=True,
                        vmin=0,
                        vmax=1.0):
  df = bias_metrics_results.set_index(SUBGROUP)
  columns = []
  # Add vertical lines around all columns.
  vlines = [i * len(models) for i in range(len(metrics_list) + 1)]
  for metric in metrics_list:
    for model in models:
      columns.append(column_name(model, metric))
  num_rows = len(df)
  num_columns = len(columns)
  fig = plt.figure(figsize=(num_columns, 0.5 * num_rows))
  ax = sns.heatmap(
      df[columns],
      annot=True,
      fmt='.2',
      cbar=False,
      cmap=cmap,
      vmin=vmin,
      vmax=vmax)
  ax.xaxis.tick_top()
  if not show_subgroups:
    ax.yaxis.set_visible(False)
  ax.yaxis.set_label_text('')
  plt.xticks(rotation=90)
  ax.vlines(vlines, *ax.get_ylim())
  if out:
    # Note: Saving as PNG causes larger file sizes compared with SVGs, but with
    # large reports, browsers don't handle all the SVGs on a single page very
    # well. We should consider using HTML tables instead, using
    # DataFrame.style.applymap for styling the table background color.
    save_inline_png(fig, out, bbox_inches='tight')
    plt.close()
  return ax


def plot_auc_heatmap(bias_metrics_results, models,
                     color_palette=None, out=None):
  if not color_palette:
    # Hack to align these colors with the AEG colors below.
    cmap = sns.color_palette('coolwarm', 9)[4:]
    cmap.reverse()
  else:
    cmap = color_palette
  return plot_metric_heatmap(
      bias_metrics_results, models, AUCS, out,
      cmap=cmap, show_subgroups=True, vmin=0.5, vmax=1.0)


def plot_aeg_heatmap(bias_metrics_results, models,
                     color_palette=None, out=None):
  if not color_palette:
    # Hack to align these colors with the AEG colors below.
    cmap = sns.color_palette('coolwarm', 7)
  else:
    cmap = color_palette
  return plot_metric_heatmap(
      bias_metrics_results, models, AEGS, out,
      cmap=cmap, show_subgroups=False, vmin=-0.5, vmax=0.5)
