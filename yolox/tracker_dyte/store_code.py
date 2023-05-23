import pickle
import numpy as np
import os
import cv2
import os.path as osp
import copy
import torch
import torch.nn.functional as F
from copy import deepcopy
from sklearn.cluster import DBSCAN

# from yolox.fast_reid.demo.visualize_result import setup_cfg
# from yolox.fast_reid.demo.predictor import FeatureExtractionDemo

# from .kalman_filter import KalmanFilter
from .extend_kalman_filter import ExtendKalmanFilter as KalmanFilter, xywh2xywh
from .extend_kalman_filter import chi2inv95
from yolox.tracker import matching
from .basetrack import BaseTrack, TrackState

from fast_reid.fast_reid_interfece import FastReIDInterface
# from .utils import IOUCLRTracker

class STrack(BaseTrack):
    # shared_kalman = KalmanFilter()
    def __init__(self, tlwh, score, did, frame_id, image, cluster_id=-1):
        # wait activate
        self._tlwh = np.asarray(tlwh, dtype=np.float)
        self.kalman_filter = None
        self.mean, self.covariance, self.last_covariance, self.alpha = None, None, None, 1.0
        self.is_activated = False
        self.last_frame = frame_id
        self.last_detection = self._tlwh.copy()

        x1, y1, x2, y2 = int(max(self._tlwh[0], 0)), int(max(self._tlwh[1], 0)), \
                         int(self._tlwh[0] + self._tlwh[2]), int(self._tlwh[1] + self._tlwh[3])

        self.last_appearance = image[y1:y2, x1:x2, :]
        self.appearance_feature = None

        self.score = score
        self.tracklet_len = 0
        self.detection_id = did
        self.cluster_id = cluster_id

    def set_appearance_feature(self, feature):
        self.appearance_feature = feature

    def set_detection_info(self, conf, ious):
        self.conf = conf
        self.ious = ious

    def predict(self):
        mean_state = self.mean.copy()
        if self.state != TrackState.Tracked:
            mean_state[7] = 0
        self.mean, self.covariance = self.kalman_filter.predict(mean_state, self.covariance)

    def update_last_frame(self, frame_id):
        self.last_frame = frame_id

    @staticmethod
    def multi_predict(stracks):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_covariance = np.asarray([st.covariance for st in stracks])
            multi_alphas = np.asarray([st.alpha for st in stracks])
            for i, st in enumerate(stracks):
                if st.state != TrackState.Tracked:
                    multi_mean[i][7] = 0
            multi_mean, multi_covariance, multi_last_covariance = STrack.shared_kalman.multi_predict(multi_mean, multi_covariance, multi_alphas)
            for i, (mean, cov, last_cov) in enumerate(zip(multi_mean, multi_covariance, multi_last_covariance)):
                stracks[i].mean = mean
                stracks[i].covariance = cov
                stracks[i].last_covariance = last_cov

    def activate(self, kalman_filter, frame_id):
        """Start a new tracklet"""
        self.kalman_filter = kalman_filter
        self.track_id = self.next_id()
        self.mean, self.covariance = self.kalman_filter.initiate(self.tlwh_to_xcywh(self._tlwh))

        self.last_covariance = self.covariance.copy()

        self.tracklet_len = 0
        self.state = TrackState.Tracked
        if frame_id == 1:
            self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id
        self.have_re_activate = False

    def re_activate(self, new_track, frame_id, new_id=False):
        self.mean, self.covariance, self.alpha = self.kalman_filter.update(self.mean, self.last_covariance, self.tlwh_to_xcywh(new_track.tlwh), self.alpha)
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()
        self.score = new_track.score
        self.detection_id = new_track.detection_id
        self.last_detection = new_track.tlwh.copy()
        self.last_appearance = new_track.last_appearance
        self.have_re_activate = True

    def update(self, new_track, frame_id):
        """
        Update a matched track
        :type new_track: STrack
        :type frame_id: int
        :type update_feature: bool
        :return:
        """
        self.frame_id = frame_id
        self.tracklet_len += 1

        new_tlwh = new_track.tlwh
        # detect_info = [new_track.conf, new_track.ious]
        self.mean, self.covariance, self.alpha = self.kalman_filter.update(self.mean, self.last_covariance, self.tlwh_to_xcywh(new_tlwh), self.alpha)

        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score
        self.detection_id = new_track.detection_id
        self.last_detection = new_track.tlwh.copy()
        self.last_appearance = new_track.last_appearance

    @property
    # @jit(nopython=True)
    def tlwh(self):
        """Get current position in bounding box format `(top left x, top left y,
                width, height)`.
        """
        if self.mean is None:
            return self._tlwh.copy()
        # ret = self.mean[:4].copy()
        # ret[2] *= ret[3]
        # ret[:2] -= ret[2:] / 2
        ret = self.xywh2xywh(self.mean[:4].copy())
        return ret

    @property
    # @jit(nopython=True)
    def tlbr(self):
        """Convert bounding box to format `(min x, min y, max x, max y)`, i.e.,
        `(top left, bottom right)`.
        """
        ret = self.tlwh.copy()
        ret[2:] += ret[:2]
        return ret

    @staticmethod
    def xywh2xywh(mean):
        x_c, y, w, h = mean
        x_c, y, w, h = float(x_c), float(y), float(w), float(h)
        return np.array([x_c - w / 2, y - h, w, h])

    @staticmethod
    # @jit(nopython=True)
    def tlwh_to_xyah(tlwh):
        """Convert bounding box to format `(center x, center y, aspect ratio,
        height)`, where the aspect ratio is `width / height`.
        """
        ret = np.asarray(tlwh).copy()
        ret[:2] += ret[2:] / 2
        ret[2] /= ret[3]
        return ret
    
    @staticmethod
    def tlwh_to_xcywh(tlwh):
        """Convert bounding box to format `(center x, center y, width, height)`.
        """
        ret = np.asarray(tlwh).copy()
        ret[0] += ret[2] / 2
        ret[1] += ret[3]
        return ret

    def to_xyah(self):
        return self.tlwh_to_xyah(self.tlwh)

    @staticmethod
    # @jit(nopython=True)
    def tlbr_to_tlwh(tlbr):
        ret = np.asarray(tlbr).copy()
        ret[2:] -= ret[:2]
        return ret

    @staticmethod
    # @jit(nopython=True)
    def tlwh_to_tlbr(tlwh):
        ret = np.asarray(tlwh).copy()
        ret[2:] += ret[:2]
        return ret

    def __repr__(self):
        return 'OT_{}_({}-{})'.format(self.track_id, self.start_frame, self.end_frame)

class DYTETracker(object):
    def __init__(self, args, video_name, frame_rate=30):
        self.tracked_stracks = []  # type: list[STrack]
        self.lost_stracks = []  # type: list[STrack]
        self.removed_stracks = []  # type: list[STrack]

        self.first_frame = False
        self.frame_id = 0
        self.args = args
        # self.det_thresh = args.track_thresh
        self.det_thresh = args.track_thresh + 0.1
        self.tracked_segment = args.tracked_segment
        # No using for sampling rate
        frame_rate = frame_rate // args.sampling_rate
        self.buffer_size = int(frame_rate / 30.0 * args.track_buffer)
        self.max_time_lost = self.buffer_size
        self.frame_info, self.object_info, self.init_mean_dict = matching.get_gt_info(video_name, args.sampling_rate)

        self.kalman_filter = KalmanFilter(self.args.sampling_rate, self.args.stdp, self.args.stdv, self.args.stda, self.args.adjusted_gate)
        self.total, self.topk = 0, 0

        self.encoder = FastReIDInterface(args.fast_reid_config, args.fast_reid_weights, 'cuda:1')
        self.proximity_thresh = args.proximity_thresh
        self.appearance_thresh = args.appearance_thresh
        
        self.chosen_sampling = args.chosen_sampling
        self.code = args.code

    def update(self, output_results, img_info, img_size, frame_id, raw_img):
        self.frame_id += 1
        STrack.shared_kalman = KalmanFilter(self.args.sampling_rate, self.args.stdp, self.args.stdv, self.args.stda, self.args.adjusted_gate)
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        if output_results.shape[1] == 5:
            scores = output_results[:, 4]
            bboxes = output_results[:, :4]
        else:
            output_results = output_results.cpu().numpy()
            scores = output_results[:, 4] * output_results[:, 5]
            bboxes = output_results[:, :4]  # x1y1x2y2
        img_h, img_w = img_info[0], img_info[1]
        scale = min(img_size[0] / float(img_h), img_size[1] / float(img_w))
        bboxes /= scale

        remain_inds = scores > self.args.track_thresh
        inds_low = scores > 0.1
        inds_high = scores < self.args.track_thresh

        inds_second = np.logical_and(inds_low, inds_high)
        dets_second = bboxes[inds_second]
        dets = bboxes[remain_inds]
        scores_keep = scores[remain_inds]
        scores_second = scores[inds_second]

        high_mapping, low_mapping = [], []
        for ind, flag in enumerate(remain_inds):
            if flag:
                high_mapping.append(ind)
        
        for ind, flag in enumerate(inds_second):
            if flag:
                low_mapping.append(ind)

        if len(dets) > 0:
            '''Detections'''
            detections = [STrack(STrack.tlbr_to_tlwh(tlbr), s, hdi, frame_id, raw_img) for (tlbr, s, hdi) in zip(dets, scores_keep, high_mapping)]
        else:
            detections = []

        last_detections = copy.deepcopy(detections)
        if len(dets_second) > 0:
            '''Detections'''
            detections_second = [STrack(STrack.tlbr_to_tlwh(tlbr), s, lid, frame_id, raw_img) for
                                 (tlbr, s, lid) in zip(dets_second, scores_second, low_mapping)]
        else:
            detections_second = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        first_stracks = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)
            if track.tracklet_len == 0 and not track.have_re_activate:
                first_stracks.append(track)
            
        if len(first_stracks) > 0 and self.args.sampling_rate > self.chosen_sampling:
            first_strack_tlbrs = [track.tlbr for track in first_stracks]
            detection_tlbrs = [track.tlbr for track in detections]
            first_distances = 1.0 - matching.ciou_batch(first_strack_tlbrs, detection_tlbrs)
            need_extract_detection_ids, near_dict = [], {}
            for i in range(len(first_stracks)):
                ids = np.argpartition(first_distances[i], min(5, len(first_distances[i]) - 1))[:min(5, len(first_distances[i]) - 1)]
                need_extract_detection_ids.extend(ids)
                near_dict[first_stracks[i].track_id] = ids
            need_extract_detection_ids = list(set(need_extract_detection_ids))
            need_detections = [detections[i] for i in need_extract_detection_ids]
            need_extract_detections = np.asarray([detect.tlbr for detect in need_detections])
            need_extract_features = self.encoder.inference(raw_img, need_extract_detections)

            for need_i in range(len(need_extract_detection_ids)):
                detections[need_extract_detection_ids[need_i]].set_appearance_feature(need_extract_features[need_i])

            for first_track in first_stracks:
                near_detection_ids = list(near_dict[first_track.track_id])
                near_detection_ids_remap = [need_extract_detection_ids.index(i) for i in near_detection_ids]
                near_detections_for_first_track = [need_detections[i] for i in near_detection_ids_remap]
                emb_dists = matching.embedding_distance([first_track], near_detections_for_first_track) / 2.0
                the_first_match_emb_id = int(np.argmin(emb_dists[0]))
                if emb_dists[0][the_first_match_emb_id] < self.appearance_thresh:
                    the_first_match_id = near_detection_ids[the_first_match_emb_id]
                    the_first_match = detections[the_first_match_id]
                    # ! initial the first track
                    first_track.mean[:4] = STrack.tlwh_to_xcywh(the_first_match.tlwh.copy()) * self.code + first_track.mean[:4].copy() * (1.0 - self.code)
                    first_track.mean[4:8] = STrack.tlwh_to_xcywh(the_first_match.tlwh.copy()) - first_track.mean[:4].copy()
                
        ''' Step 2: First association, with high score detection boxes '''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks)
        STrack.multi_predict(strack_pool)
        
        # Predict the current location with KF
        dists = matching.ciou_distance(strack_pool, detections)

        if not self.args.mot20:
            dists = matching.fuse_score(dists, detections)

        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=self.args.match_thresh_d1)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]

            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)
            track.update_last_frame(frame_id)

        ''' Step 3: Second association, with low score detection boxes '''
        # association the untrack to the low score detections

        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections_second)
        matches, u_track, u_detection_second = matching.linear_assignment(dists, thresh=self.args.match_thresh_d2)
        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)
            track.update_last_frame(frame_id)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        """ Deal with unconfirmed tracks, usually tracks with only one beginning frame """
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)

        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=self.args.match_thresh_d3)
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
            track.update_last_frame(frame_id)

        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks """
        # TODO: extract feature of new track
        if self.args.sampling_rate > self.chosen_sampling:
            init_dets = np.asarray([detections[i].tlbr for i in u_detection])
            features_keep = self.encoder.inference(raw_img, init_dets)
        for u_i, inew in enumerate(u_detection):
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            
            track.activate(self.kalman_filter, self.frame_id)
            if self.args.sampling_rate > self.chosen_sampling:
                feature = features_keep[u_i]
                track.set_appearance_feature(feature)
            activated_starcks.append(track)

        """ Step 5: Update state """
        for track in self.lost_stracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks = [t for t in self.tracked_stracks if t.state == TrackState.Tracked]
        self.tracked_stracks = joint_stracks(self.tracked_stracks, activated_starcks)
        self.tracked_stracks = joint_stracks(self.tracked_stracks, refind_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.tracked_stracks)
        self.lost_stracks.extend(lost_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.removed_stracks)
        self.removed_stracks.extend(removed_stracks)
        self.tracked_stracks, self.lost_stracks = remove_duplicate_stracks(self.tracked_stracks, self.lost_stracks)
        # get scores of lost tracks
        output_stracks = [track for track in self.tracked_stracks if track.is_activated]

        self.last_detections = last_detections

        return output_stracks, bboxes, scores


def joint_stracks(tlista, tlistb):
    exists = {}
    res = []
    for t in tlista:
        exists[t.track_id] = 1
        res.append(t)
    for t in tlistb:
        tid = t.track_id
        if not exists.get(tid, 0):
            exists[tid] = 1
            res.append(t)
    return res


def sub_stracks(tlista, tlistb):
    stracks = {}
    for t in tlista:
        stracks[t.track_id] = t
    for t in tlistb:
        tid = t.track_id
        if stracks.get(tid, 0):
            del stracks[tid]
    return list(stracks.values())


def remove_duplicate_stracks(stracksa, stracksb):
    pdist = matching.iou_distance(stracksa, stracksb)
    pairs = np.where(pdist < 0.15)
    dupa, dupb = list(), list()
    for p, q in zip(*pairs):
        timep = stracksa[p].frame_id - stracksa[p].start_frame
        timeq = stracksb[q].frame_id - stracksb[q].start_frame
        if timep > timeq:
            dupb.append(q)
        else:
            dupa.append(p)
    resa = [t for i, t in enumerate(stracksa) if not i in dupa]
    resb = [t for i, t in enumerate(stracksb) if not i in dupb]
    return resa, resb