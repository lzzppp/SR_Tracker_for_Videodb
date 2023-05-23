import cv2
import math
import numpy as np
import scipy
import torch
import lap
import torch.nn.functional as F
from scipy.spatial.distance import cdist

from cython_bbox import bbox_overlaps as bbox_ious
from yolox.tracker import kalman_filter
import time
from sklearn import preprocessing

def ciou_batch(bboxes1, bboxes2):
    """
    :param bbox_p: predict of bbox(N,4)(x1,y1,x2,y2)
    :param bbox_g: groundtruth of bbox(N,4)(x1,y1,x2,y2)
    :return:
    """
    ious = np.zeros((len(bboxes1), len(bboxes2)), dtype=np.float)
    if ious.size == 0:
        return ious
    
    # for details should go to https://arxiv.org/pdf/1902.09630.pdf
    # ensure predict's bbox form
    bboxes2 = np.expand_dims(bboxes2, 0)
    bboxes1 = np.expand_dims(bboxes1, 1)

    # calculate the intersection box
    xx1 = np.maximum(bboxes1[..., 0], bboxes2[..., 0])
    yy1 = np.maximum(bboxes1[..., 1], bboxes2[..., 1])
    xx2 = np.minimum(bboxes1[..., 2], bboxes2[..., 2])
    yy2 = np.minimum(bboxes1[..., 3], bboxes2[..., 3])
    w = np.maximum(0., xx2 - xx1)
    h = np.maximum(0., yy2 - yy1)
    wh = w * h
    iou = wh / ((bboxes1[..., 2] - bboxes1[..., 0]) * (bboxes1[..., 3] - bboxes1[..., 1])                                      
        + (bboxes2[..., 2] - bboxes2[..., 0]) * (bboxes2[..., 3] - bboxes2[..., 1]) - wh) 

    centerx1 = (bboxes1[..., 0] + bboxes1[..., 2]) / 2.0
    centery1 = (bboxes1[..., 1] + bboxes1[..., 3]) / 2.0
    centerx2 = (bboxes2[..., 0] + bboxes2[..., 2]) / 2.0
    centery2 = (bboxes2[..., 1] + bboxes2[..., 3]) / 2.0

    inner_diag = (centerx1 - centerx2) ** 2 + (centery1 - centery2) ** 2

    xxc1 = np.minimum(bboxes1[..., 0], bboxes2[..., 0])
    yyc1 = np.minimum(bboxes1[..., 1], bboxes2[..., 1])
    xxc2 = np.maximum(bboxes1[..., 2], bboxes2[..., 2])
    yyc2 = np.maximum(bboxes1[..., 3], bboxes2[..., 3])

    outer_diag = (xxc2 - xxc1) ** 2 + (yyc2 - yyc1) ** 2
    
    w1 = bboxes1[..., 2] - bboxes1[..., 0]
    h1 = bboxes1[..., 3] - bboxes1[..., 1]
    w2 = bboxes2[..., 2] - bboxes2[..., 0]
    h2 = bboxes2[..., 3] - bboxes2[..., 1]

    # prevent dividing over zero. add one pixel shift
    h2 = h2 + 1.
    h1 = h1 + 1.
    arctan = np.arctan(w2/h2) - np.arctan(w1/h1)
    v = (4 / (np.pi ** 2)) * (arctan ** 2)
    S = 1 - iou 
    alpha = v / (S+v)
    ciou = iou - inner_diag / outer_diag - alpha * v
    
    return (ciou + 1) / 2.0 # resize from (-1,1) to (0,1)

def find_topk(a, k, axis=-1, largest=False, sorted=True):
    if axis is None:
        axis_size = a.size
    else:
        axis_size = a.shape[axis]
    assert 1 <= k <= axis_size

    a = np.asanyarray(a)
    if largest:
        index_array = np.argpartition(a, axis_size-k, axis=axis)
        topk_indices = np.take(index_array, -np.arange(k)-1, axis=axis)
    else:
        index_array = np.argpartition(a, k-1, axis=axis)
        topk_indices = np.take(index_array, np.arange(k), axis=axis)
    topk_values = np.take_along_axis(a, topk_indices, axis=axis)
    if sorted:
        sorted_indices_in_topk = np.argsort(topk_values, axis=axis)
        if largest:
            sorted_indices_in_topk = np.flip(sorted_indices_in_topk, axis=axis)
        sorted_topk_values = np.take_along_axis(
            topk_values, sorted_indices_in_topk, axis=axis)
        sorted_topk_indices = np.take_along_axis(
            topk_indices, sorted_indices_in_topk, axis=axis)
        return sorted_topk_values, sorted_topk_indices
    return topk_values, topk_indices

def get_k_min(array, k):
    values, indices = find_topk(array, k, axis=-1, largest=False, sorted=False)
    # _k_sort = np.argpartition(array, k)[:k].tolist()  # 最小的k个数据的下标
    return list(indices)

def dmd(X1, X2, rank):
    """Dynamic Mode Decomposition, DMD."""
    
    u, s, v = np.linalg.svd(X1, full_matrices = 0)
    A_tilde = u[:, : rank].conj().T @ X2 @ v[: rank, :].conj().T @ np.linalg.inv(np.diag(s[: rank]))
    eigval, eigvec = np.linalg.eig(A_tilde)
    Phi = X2 @ v[: rank, :].conj().T @ np.linalg.inv(np.diag(s[: rank])) @ eigvec
    temp = Phi @ np.diag(eigval) @ (np.linalg.pinv(Phi) @ X1)
    
    return temp.real, eigval, Phi

def merge_matches(m1, m2, shape):
    O,P,Q = shape
    m1 = np.asarray(m1)
    m2 = np.asarray(m2)

    M1 = scipy.sparse.coo_matrix((np.ones(len(m1)), (m1[:, 0], m1[:, 1])), shape=(O, P))
    M2 = scipy.sparse.coo_matrix((np.ones(len(m2)), (m2[:, 0], m2[:, 1])), shape=(P, Q))

    mask = M1*M2
    match = mask.nonzero()
    match = list(zip(match[0], match[1]))
    unmatched_O = tuple(set(range(O)) - set([i for i, j in match]))
    unmatched_Q = tuple(set(range(Q)) - set([j for i, j in match]))

    return match, unmatched_O, unmatched_Q


def _indices_to_matches(cost_matrix, indices, thresh):
    matched_cost = cost_matrix[tuple(zip(*indices))]
    matched_mask = (matched_cost <= thresh)

    matches = indices[matched_mask]
    unmatched_a = tuple(set(range(cost_matrix.shape[0])) - set(matches[:, 0]))
    unmatched_b = tuple(set(range(cost_matrix.shape[1])) - set(matches[:, 1]))

    return matches, unmatched_a, unmatched_b


def linear_assignment(cost_matrix, thresh):
    if cost_matrix.size == 0:
        return np.empty((0, 2), dtype=int), tuple(range(cost_matrix.shape[0])), tuple(range(cost_matrix.shape[1]))
    matches, unmatched_a, unmatched_b = [], [], []
    cost, x, y = lap.lapjv(cost_matrix, extend_cost=True, cost_limit=thresh)
    for ix, mx in enumerate(x):
        if mx >= 0:
            matches.append([ix, mx])
    unmatched_a = np.where(x < 0)[0]
    unmatched_b = np.where(y < 0)[0]
    matches = np.asarray(matches)
    return matches, unmatched_a, unmatched_b


def cs_ious(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if ious.size == 0:
        return ious

    ious = cdist(
        np.ascontiguousarray(atlbrs, dtype=np.float),
        np.ascontiguousarray(btlbrs, dtype=np.float),
        "euclidean"
    )

    return ious

def normalize_adj(mx):
    """Row-normalize sparse matrix"""
    rowsum = np.array(mx.sum(1))
    r_inv_sqrt = np.power(rowsum, -0.5).flatten()
    r_inv_sqrt[np.isinf(r_inv_sqrt)] = 0.
    r_mat_inv_sqrt = np.diag(r_inv_sqrt)
    return mx.dot(r_mat_inv_sqrt).transpose().dot(r_mat_inv_sqrt)

def intersection_over_union(box1, box2, wh=False):
    """
    计算IoU（交并比）
    :param box1: bounding box1
    :param box2: bounding box2
    :param wh: 坐标的格式是否为（x,y,w,h）
    :return:计算结果
    """
    if not wh:
        xmin1, ymin1, xmax1, ymax1 = box1
        xmin2, ymin2, xmax2, ymax2 = box2
    else:
        xmin1, ymin1 = int(box1[0] - box1[2] / 2.0), int(box1[1] - box1[3] / 2.0)
        xmax1, ymax1 = int(box1[0] + box1[2] / 2.0), int(box1[1] + box1[3] / 2.0)
        xmin2, ymin2 = int(box2[0] - box2[2] / 2.0), int(box2[1] - box2[3] / 2.0)
        xmax2, ymax2 = int(box2[0] + box2[2] / 2.0), int(box2[1] + box2[3] / 2.0)
    # 获取矩形框交集对应的左上角和右下角的坐标（intersection）
    xx1 = max([xmin1, xmin2])
    yy1 = max([ymin1, ymin2])
    xx2 = min([xmax1, xmax2])
    yy2 = min([ymax1, ymax2])
    # 计算两个矩形框面积
    area1 = (xmax1 - xmin1) * (ymax1 - ymin1)
    area2 = (xmax2 - xmin2) * (ymax2 - ymin2)
    inter_area = (max([0, xx2 - xx1])) * (max([0, yy2 - yy1]))  # 计算交集面积
    iou = inter_area / (area1 + area2 - inter_area + 1e-6)  # 计算交并比
    return iou

def c_iou(rec1, rec2):
    """
    计算CIoU
    :param rec1: bounding box1（xmin, ymin, xmax, ymax）格式
    :param rec2: bounding box2（xmin, ymin, xmax, ymax）格式
    :return: 计算结果
    """
    # xmin, ymin, xmax, ymax格式
    xmin1, ymin1, xmax1, ymax1 = rec1
    xmin2, ymin2, xmax2, ymax2 = rec2
    iou = intersection_over_union(rec1, rec2)
    # 中心点距离平方
    center1 = ((xmin1 + xmax1) / 2, (ymin1 + ymax1) / 2)
    center2 = ((xmin2 + xmax2) / 2, (ymin2 + ymax2) / 2)
    d_center2 = (center1[0] - center2[0]) ** 2 + (center1[1] - center2[1]) ** 2
    # 最小闭包矩形对角线长度平方
    corner1 = (min(xmin1, xmax1, xmin2, xmax2), min(ymin1, ymax1, ymin2, ymax2))
    corner2 = (max(xmin1, xmax1, xmin2, xmax2), max(ymin1, ymax1, ymin2, ymax2))
    d_corner2 = (corner1[0] - corner2[0]) ** 2 + (corner1[1] + corner2[1]) ** 2
    w1, h1 = xmax1 - xmin1, ymax1 - ymin1
    w2, h2 = xmax2 - xmin2, ymax2 - ymin2
    # 度量长宽比的参数
    v = 4 * (np.arctan(w1 / h1) - np.arctan(w2 / h2)) ** 2 / (np.pi ** 2)
    alpha = v / (1 - iou + v)
    ciou = iou - d_center2 / d_corner2 - alpha * v
    return ciou

def bboxes_ciou(bboxes1, bboxes2):
    _cious = []
    for bbox1 in bboxes1:
        _cious.append([c_iou(bbox1, bbox2) for bbox2 in bboxes2])
    _cious = np.array(_cious)
    return _cious

def iou_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.tlbr for track in atracks]
        btlbrs = [track.tlbr for track in btracks]
    _ious = ious(atlbrs, btlbrs)
    cost_matrix = 1 - _ious

    return cost_matrix

def cious(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if ious.size == 0:
        return ious

    ious = bboxes_ciou(
        np.ascontiguousarray(atlbrs, dtype=np.float),
        np.ascontiguousarray(btlbrs, dtype=np.float)
    )

    return ious

def ciou_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.tlbr for track in atracks]
        btlbrs = [track.tlbr for track in btracks]
    _ious = cious(atlbrs, btlbrs)
    cost_matrix = 1 - _ious

    return cost_matrix

def cal_features(atlbrs, btlbrs, t=0.1):
    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if ious.size == 0:
        return ious

    hps = torch.stack(atlbrs)
    graph_features = torch.stack(btlbrs)

    gf = F.normalize(torch.relu(graph_features), p=2, dim=1)
    hf = F.normalize(torch.relu(hps), p=2, dim=1)
    features = torch.mm(gf, hf.t()).transpose(1, 0)
    features_exp = torch.exp(features / t)
    features_sum = torch.sum(features_exp, dim=1, keepdim=True)
    features_log = (features_exp / features_sum).cpu().numpy()

    return features_log

def feature_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.hidden for track in atracks]
        btlbrs = [track.feature for track in btracks]
    _features = cal_features(atlbrs, btlbrs)
    cost_matrix = 1 - _features

    return cost_matrix

def detection_iou_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.dtlbr for track in atracks]
        btlbrs = [track.dtlbr for track in btracks]
    _ious = ious(atlbrs, btlbrs)
    cost_matrix = 1 - _ious

    return cost_matrix

def EuclideanDistances(A, B):
    BT = B.transpose()
    # vecProd = A * BT
    vecProd = np.dot(A,BT)
    # print(vecProd)
    SqA =  A**2
    # print(SqA)
    sumSqA = np.matrix(np.sum(SqA, axis=1))
    sumSqAEx = np.tile(sumSqA.transpose(), (1, vecProd.shape[1]))
    # print(sumSqAEx)
 
    SqB = B**2
    sumSqB = np.sum(SqB, axis=1)
    sumSqBEx = np.tile(sumSqB, (vecProd.shape[0], 1))    
    SqED = sumSqBEx + sumSqAEx - 2*vecProd
    SqED[SqED<0]=0.0   
    matrix = np.sqrt(SqED)
    return np.array(matrix)

def calculate_distance(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if ious.size == 0:
        return ious

    atlbrs = np.ascontiguousarray(atlbrs, dtype=np.float64)
    btlbrs = np.ascontiguousarray(btlbrs, dtype=np.float64)

    # atlbrs = preprocessing.normalize(atlbrs, norm='l2')
    # btlbrs = preprocessing.normalize(btlbrs, norm='l2')

    # distance = np.dot(atlbrs, btlbrs)
    distance = EuclideanDistances(atlbrs, btlbrs)

    return distance

def iou_distance_by_observation(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.history for track in atracks]
        btlbrs = [track.observation for track in btracks]
    _ious = calculate_distance(atlbrs, btlbrs)
    cost_matrix = np.array(_ious / 2.0)

    return cost_matrix

def bbox_squares(squares1, squares2):
    # squares1 = np.expand_dims(squares1, axis=1)
    # squares2 = np.expand_dims(squares2, axis=1)
    squares1_expand = np.tile(np.expand_dims(squares1, axis=1), (1, squares2.shape[0]))
    squares2_expand = np.tile(np.expand_dims(squares2, axis=1), (1, squares1.shape[0])).transpose(1, 0)

    # print(squares1.shape, squares2.shape, squares1_expand.shape, squares2_expand.shape)
    squares = (squares1_expand + squares2_expand - np.abs(squares1_expand - squares2_expand)) / (squares1_expand + squares2_expand + np.abs(squares1_expand - squares2_expand))

    return squares

def squares(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    _squares = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if _squares.size == 0:
        return _squares

    _squares = bbox_squares(
        np.ascontiguousarray(atlbrs, dtype=np.float),
        np.ascontiguousarray(btlbrs, dtype=np.float)
    )

    return _squares

def square_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.tlwh[2] * track.tlwh[3] for track in atracks]
        btlbrs = [track.tlwh[2] * track.tlwh[3] for track in btracks] # track.tlwh[2] * 
    _squares = squares(atlbrs, btlbrs)
    cost_matrix = 1 - _squares

    return cost_matrix

def bbox_moves(squares1, squares2):
    squares1_expand = np.tile(np.expand_dims(squares1, axis=1), (1, squares2.shape[0], 1))
    squares2_expand = np.tile(np.expand_dims(squares2, axis=1), (1, squares1.shape[0], 1)).transpose(1, 0, 2)

    move_vectors = squares2_expand - squares1_expand

    return move_vectors

def moves(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    _moves = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if _moves.size == 0:
        return _moves

    _moves = bbox_moves(
        np.ascontiguousarray(atlbrs, dtype=np.float),
        np.ascontiguousarray(btlbrs, dtype=np.float)
    )

    return _moves


def move_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.tlbr for track in atracks]
        btlbrs = [track.tlbr for track in btracks]
    _squares = moves(atlbrs, btlbrs)
    move_distances = []
    for tracki in range(len(atlbrs)):
        _square = _squares[tracki]
        last_move = np.expand_dims(np.array(atracks[tracki].tlbr), axis=0)
        last_move = preprocessing.normalize(last_move, norm='l2').transpose(1, 0)
        _square = preprocessing.normalize(_square, norm='l2')
        move_distance = np.dot(_square, last_move)[:, 0]
        move_distances.append(move_distance)
    
    move_distances = np.array(move_distances)

    cost_matrix = (1 - move_distances) / 2.0

    return cost_matrix


def v_iou_distance(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [track.tlwh_to_tlbr(track.pred_bbox) for track in atracks]
        btlbrs = [track.tlwh_to_tlbr(track.pred_bbox) for track in btracks]
    _ious = ious(atlbrs, btlbrs)
    cost_matrix = 1 - _ious

    return cost_matrix


def embedding_distance(tracks, detections, metric='cosine'):
    """
    :param tracks: list[STrack]
    :param detections: list[BaseTrack]
    :param metric:
    :return: cost_matrix np.ndarray
    """

    cost_matrix = np.zeros((len(tracks), len(detections)), dtype=np.float)
    if cost_matrix.size == 0:
        return cost_matrix
    det_features = np.asarray([track.appearance_feature for track in detections], dtype=np.float)
    #for i, track in enumerate(tracks):
        #cost_matrix[i, :] = np.maximum(0.0, cdist(track.smooth_feat.reshape(1,-1), det_features, metric))
    track_features = np.asarray([track.appearance_feature for track in tracks], dtype=np.float)
    cost_matrix = np.maximum(0.0, cdist(track_features, det_features, metric))  # Nomalized features
    return cost_matrix


def gate_cost_matrix(kf, cost_matrix, tracks, detections, only_position=False):
    if cost_matrix.size == 0:
        return cost_matrix
    gating_dim = 2 if only_position else 4
    gating_threshold = kalman_filter.chi2inv95[gating_dim]
    measurements = np.asarray([det.to_xyah() for det in detections])
    for row, track in enumerate(tracks):
        gating_distance = kf.gating_distance(
            track.mean, track.covariance, measurements, only_position)
        cost_matrix[row, gating_distance > gating_threshold] = np.inf
    return cost_matrix


def fuse_motion(kf, cost_matrix, tracks, detections, only_position=False, lambda_=0.98):
    if cost_matrix.size == 0:
        return cost_matrix
    gating_dim = 2 if only_position else 4
    gating_threshold = kalman_filter.chi2inv95[gating_dim]
    measurements = np.asarray([det.to_xyah() for det in detections])
    for row, track in enumerate(tracks):
        gating_distance = kf.gating_distance(
            track.mean, track.covariance, measurements, only_position, metric='maha')
        cost_matrix[row, gating_distance > gating_threshold] = np.inf
        cost_matrix[row] = lambda_ * cost_matrix[row] + (1 - lambda_) * gating_distance
    return cost_matrix

def trust_detector(tracks, detections, cost_matrix, threshold=0.5, weight=0.1, score_threshold=0.5):
    if cost_matrix.size == 0:
        return cost_matrix
    for itrack, track in enumerate(tracks):
        for idetection, detection in enumerate(detections):
            cost = cost_matrix[itrack, idetection]
            if cost < threshold:
                cost_matrix[itrack, idetection] = cost_matrix[itrack, idetection] * (1.0 - weight) + abs(track.score - detection.score) * weight
            
    return cost_matrix

def fuse_iou(cost_matrix, tracks, detections):
    if cost_matrix.size == 0:
        return cost_matrix
    reid_sim = 1 - cost_matrix
    iou_dist = iou_distance(tracks, detections)
    iou_sim = 1 - iou_dist
    fuse_sim = reid_sim * (1 + iou_sim) / 2
    det_scores = np.array([det.score for det in detections])
    det_scores = np.expand_dims(det_scores, axis=0).repeat(cost_matrix.shape[0], axis=0)
    #fuse_sim = fuse_sim * (1 + det_scores) / 2
    fuse_cost = 1 - fuse_sim
    return fuse_cost


def fuse_score(cost_matrix, detections):
    if cost_matrix.size == 0:
        return cost_matrix
    iou_sim = 1 - cost_matrix
    det_scores = np.array([det.score for det in detections])
    det_scores = np.expand_dims(det_scores, axis=0).repeat(cost_matrix.shape[0], axis=0)

    fuse_sim = iou_sim * det_scores
    fuse_cost = 1 - fuse_sim
    return fuse_cost


def relation_distance(matrix1, matrix2):
    """
    :param matrix1: np.ndarray
    :param matrix2: np.ndarray
    :return:
    """
    if matrix1.size == 0 or matrix2.size == 0:
        return np.zeros((matrix1.shape[0], matrix2.shape[0]))
    
    occulued_matrix = []
    for vector1 in matrix1:
        vector1_dist = []
        for vector2 in matrix2:
            vector1_occulue_number = np.count_nonzero(vector1 == 1)
            vector1_occulued_number = np.count_nonzero(vector1 == -1)
            vector1_none_number = np.count_nonzero(vector1 == 0)

            vector2_occulue_number = np.count_nonzero(vector2 == 1)
            vector2_occulued_number = np.count_nonzero(vector2 == -1)
            vector2_none_number = np.count_nonzero(vector2 == 0)

            v1, v2 = np.array([vector1_occulue_number, vector1_occulued_number]), np.array([vector2_occulue_number, vector2_occulued_number])

            dist = np.sum(np.square(v1 - v2)) / (max(np.sqrt(np.sum(np.square(v1))), 1) * max(np.sqrt(np.sum(np.square(v2))), 1)) / 2.0
            # print(dist)
            vector1_dist.append(dist)
        occulued_matrix.append(vector1_dist)
    occulued_matrix = np.array(occulued_matrix, dtype=np.float)

    return occulued_matrix

    # return cdist(matrix1, matrix2, metric='cosine')


def linear_assignment_occlude(cost_sco,
                              cost_iou,
                              cost_squ,
                              cost_mov,
                              thresh,
                              first_threshold=0.7,
                              second_threshold=0.5,
                              third_threshold=0.5):
    if cost_iou.size == 0:
        return np.empty((0, 2), dtype=int), tuple(range(cost_iou.shape[0])), tuple(range(cost_iou.shape[1]))

    matches, unmatched_a, unmatched_b = [], [], []
    adjust_weight = 0.9

    # first filter
    # matches_track, matches_detection = [], []
    # condition_track, condition_detection = np.where(cost_iou < first_threshold) 
    # condition_cost_sco = cost_sco[condition_track, :][:, condition_detection]

    # _, x, _ = lap.lapjv(condition_cost_sco, extend_cost=True, cost_limit=thresh)
    # for ix, mx in enumerate(x):
    #     if mx >= 0:
    #         matches.append([condition_track[ix], condition_detection[mx]])
    #         matches_track.append(condition_track[ix])
    #         matches_detection.append(condition_detection[mx])
    
    # cost_iou[matches_track,:] = 1.0
    # cost_iou[:,matches_detection] = 1.0
    # cost_sco[matches_track,:] = 1.0
    # cost_sco[:,matches_detection] = 1.0
    
    # second filter
    # condition_output = np.where(cost_iou < second_threshold, 1, 0)
    # condition_detection = np.where(np.sum(condition_output, axis=0) > 1)[0]
    # condition_track = np.where(cost_iou[:, condition_detection] < second_threshold)[0]

    # if len(condition_track) != 0 and len(condition_detection) != 0:
    #     matches_track, matches_detection = [], []
    #     condition_cost_sco = cost_sco[condition_track, :][:, condition_detection] + cost_squ[condition_track, :][:, condition_detection]

    #     _, x, _ = lap.lapjv(condition_cost_sco, extend_cost=True, cost_limit=thresh)
    #     for ix, mx in enumerate(x):
    #         if mx >= 0:
    #             matches.append([condition_track[ix], condition_detection[mx]])
    #             matches_track.append(condition_track[ix])
    #             matches_detection.append(condition_detection[mx])
        
    #     cost_iou[matches_track,:] = 1.0
    #     cost_iou[:,matches_detection] = 1.0
    #     cost_sco[matches_track,:] = 1.0
    #     cost_sco[:,matches_detection] = 1.0    

    # third filter
    # condition_output = np.where(cost_iou < third_threshold, 1, 0)
    # condition_track = np.where(np.sum(condition_output, axis=1) > 1)[0]
    # condition_detection = np.where(cost_iou[condition_track, :] < third_threshold)[0]

    # if len(condition_track) != 0 and len(condition_detection) != 0:
    #     matches_track, matches_detection = [], []
    #     condition_cost_sco = cost_sco[condition_track, :][:, condition_detection] * adjust_weight + cost_mov[condition_track, :][:, condition_detection] * (1.0 - adjust_weight)

    #     _, x, _ = lap.lapjv(condition_cost_sco, extend_cost=True, cost_limit=thresh)
    #     for ix, mx in enumerate(x):
    #         if mx >= 0:
    #             matches.append([condition_track[ix], condition_detection[mx]])
    #             matches_track.append(condition_track[ix])
    #             matches_detection.append(condition_detection[mx])
        
    #     cost_iou[matches_track,:] = 1.0
    #     cost_iou[:,matches_detection] = 1.0
    #     cost_sco[matches_track,:] = 1.0
    #     cost_sco[:,matches_detection] = 1.0

    cost_sco = cost_sco * adjust_weight + cost_squ * (1 - adjust_weight)

    # fourth filter
    _matches, u_track, u_detection = linear_assignment(cost_sco, thresh=thresh)    

    matches += [_match for _match in _matches]
    matches = np.asarray(matches)

    return matches, u_track, u_detection

def judge(axy, bxy):
    if axy[-1] < bxy[-1]:
        return -1
    else:
        return 1

def make_occulued_matrix(stracks, img_w, img_h):
    ious = np.zeros((len(stracks), len(stracks)), dtype=np.float)
    if ious.size == 0:
        return ious, ious
    frame_data = [track.tlwh for track in stracks]
    features, occulued_graph, direction_graph = [], [], []
    for i, (x_i, y_i, w_i, h_i) in enumerate(frame_data):
        point_i = [(x_i + w_i / 2) / img_w, (y_i + h_i / 2) / img_h]
        ograph, dgraph = [], []
        for j, (x_j, y_j, w_j, h_j) in enumerate(frame_data):
            point_j = [(x_j + w_j / 2) / img_w, (y_j + h_j / 2) / img_h]
    
            if y_j + h_j < y_i + h_i:
                ograph.append(1)
            else:
                ograph.append(0)

            dgraph.append([point_j[0] - point_i[0], point_j[1] - point_i[1]])
        features.append([x_i / img_w, y_i / img_h, w_i / img_w, h_i / img_h])

        occulued_graph.append(ograph)
        direction_graph.append(dgraph)

    features = np.array(features).astype(np.float64)
    occulued_graph = np.array(occulued_graph).astype(np.float64)
    direction_graph = np.array(direction_graph).astype(np.float64)
    return features, occulued_graph, direction_graph

def make_occulued_matrix_detection(iou_matrix, stracks):
    strack_number = len(stracks)
    relation_matrix = []
    for strack_i in range(strack_number):
        strack_relation = []
        for strack_j in range(strack_number):
            if iou_matrix[strack_i, strack_j] < 1.0:
                strack_relation.append(judge(stracks[strack_i].tlbr, stracks[strack_j].tlbr))
            else:
                strack_relation.append(0)
        relation_matrix.append(strack_relation)
    relation_matrix = np.array(relation_matrix)
    return relation_matrix


def adjust_matches(matches, u_track, u_detection, 
                   dists, strack_relation_matrix, detection_relation_matrix, thresh=0.65):
    new_matches, new_u_track, new_u_detection = [], [], []

    track_detection_dict, detection_track_dict, track_relation_changes, detection_relation_changes, relation_changes = {}, {}, {}, {}, {}

    for match_pair in matches:
        track_id, detection_id = match_pair
        track_detection_dict[track_id] = detection_id
        detection_track_dict[detection_id] = track_id

    for strack_i, strack_relation in enumerate(strack_relation_matrix):
        strack_js = np.nonzero(strack_relation[strack_i + 1:])[0].tolist()
        for strack_j in strack_js:
            strack_ij_relation = strack_relation[strack_j]
            if strack_i in track_detection_dict and strack_j in track_detection_dict:
                new_strack_ij_relation = detection_relation_matrix[track_detection_dict[strack_i], track_detection_dict[strack_j]]
                if strack_ij_relation != new_strack_ij_relation and (strack_ij_relation != 0 and new_strack_ij_relation != 0):
                    dists[strack_i, track_detection_dict[strack_i]] += 0.1
                    dists[strack_j, track_detection_dict[strack_j]] += 0.1
                    break
                    # strack_i_iou_distance = dists[strack_i,:]
                    # for detection_j, strack_i_detection_j_dist in enumerate(strack_i_iou_distance):
                    #     if strack_i_detection_j_dist < 1.0

    matches, u_track, u_detection = linear_assignment(dists, thresh=thresh)
    
    return matches, u_track, u_detection


def remap_observation(ious, obss):
    ious = np.zeros_like(ious)
    if ious.size == 0:
        return ious

    obss_copy = np.copy(obss)
    new_obss = np.ones_like(obss)
    mask = np.logical_and(ious < 0.5, obss < 0.2)
    new_obss[mask] = obss_copy[mask]
    
    return new_obss

def map_func(x):
    w, b = 5, 8
    # y = 0.85 + 0.15/50 * x
    y1 = 0.85 + 0.15/50 * x
    # y2 = 1.0 - math.exp(-(x + b)/w)
    # y = y1 * 1.0 + y2 * 0.0
    # a = 2
    # b = math.log(0.15, a)
    # w = (math.log(0.005, a) - b) / 50
    # y = 1.0 - a**(w*x + b)
    return y1

def get_gt_info(video_name, srate):
    gt_path = f"./datasets/mot_{srate}/train/{video_name}/gt/gt_val_half.txt"
    frame_data_dict, object_data_dict = {}, {}
    with open(gt_path, "r") as f:
        lines = f.readlines()
        for line in lines:
            frame_id, object_id, x, y, w, h, is_activate, _, conf = line.rstrip("\n").split(',')
            frame_id, object_id, x, y, w, h, is_activate, conf = int(frame_id), int(object_id), float(x), float(y), float(w), float(h), int(is_activate), float(conf)
            if is_activate < 1.0:
                continue
            
            if frame_id not in frame_data_dict:
                frame_data_dict[frame_id] = []
            frame_data_dict[frame_id].append([object_id, x, y, w, h, conf])
            
            if object_id not in object_data_dict:
                object_data_dict[object_id] = []
            object_data_dict[object_id].append([frame_id, x, y, w, h, conf])
    
    init_mean_dict = {}
    for object_id, object_data in object_data_dict.items():
        object_data_dict[object_id] = sorted(object_data, key=lambda x: x[0])
        for i, (frame_id, x, y, w, h, conf) in enumerate(object_data_dict[object_id][:-1]):
            init_mean = np.zeros((10, 1))
            
            next_frame_id, next_x, next_y, next_w, next_h, next_conf = object_data_dict[object_id][i+1]
            # nnext_frame_id, nnext_x, nnext_y, nnext_w, nnext_h, nnext_conf = object_data_dict[object_id][i+2]
            
            xc, y2, w, h = x + w/2, y + h, w, h
            next_xc, next_y2, next_w, next_h = next_x + next_w/2, next_y + next_h, next_w, next_h
            # nnext_xc, nnext_y2, nnext_w, nnext_h = nnext_x + nnext_w/2, nnext_y + nnext_h, nnext_w, nnext_h
            
            veloctiy = np.array([[next_xc - xc],
                                 [next_y2 - y2], 
                                 [next_w - w], 
                                 [next_h - h]])

            acceleration = np.array([[0.0], 
                                     [0.0]])
            
            init_mean[4:8] = veloctiy
            init_mean[8:] = acceleration
            
            if object_id not in init_mean_dict:
                init_mean_dict[object_id] = {}
            init_mean_dict[object_id][frame_id] = init_mean

        # for frame_id, x, y, w, h, conf in object_data_dict[object_id][-1:]:
        #     init_mean = np.zeros((10, 1))
        #     init_mean_dict[object_id][frame_id] = init_mean
    
    return frame_data_dict, object_data_dict, init_mean_dict

def ious(axcywh, bxcywh):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    ious = np.zeros((len(axcywh), len(bxcywh)), dtype=np.float)
    if ious.size == 0:
        return ious
    
    _ious = bbox_ious(
        np.ascontiguousarray(axcywh, dtype=np.float),
        np.ascontiguousarray(bxcywh, dtype=np.float)
    )

    return _ious

def cs_ious(atlbrs, btlbrs):
    """
    Compute cost based on IoU
    :type atlbrs: list[tlbr] | np.ndarray
    :type atlbrs: list[tlbr] | np.ndarray

    :rtype ious np.ndarray
    """
    ious = np.zeros((len(atlbrs), len(btlbrs)), dtype=np.float)
    if ious.size == 0:
        return ious

    _cious = cdist(
        np.ascontiguousarray(atlbrs, dtype=np.float),
        np.ascontiguousarray(btlbrs, dtype=np.float),
        "euclidean"
    )

    return _cious


def calculate_shift(atracks, btracks):
    """
    Compute cost based on IoU
    :type atracks: list[STrack]
    :type btracks: list[STrack]

    :rtype cost_matrix np.ndarray
    """

    if (len(atracks)>0 and isinstance(atracks[0], np.ndarray)) or (len(btracks) > 0 and isinstance(btracks[0], np.ndarray)):
        atlbrs = atracks
        btlbrs = btracks
    else:
        atlbrs = [[track.tlwh_to_xcywh(track.tlwh)[0], track.tlwh_to_xcywh(track.tlwh)[1]] for track in atracks]
        btlbrs = [[track.tlwh_to_xcywh(track.tlwh)[0], track.tlwh_to_xcywh(track.tlwh)[1]] for track in btracks]
    _cs_ious = cs_ious(atlbrs, btlbrs)

    return _cs_ious