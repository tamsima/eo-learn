"""
Module for cloud masking

Credits:
Copyright (c) 2017-2019 Matej Aleksandrov, Matej Batič, Andrej Burja, Eva Erzin (Sinergise)
Copyright (c) 2017-2019 Grega Milčinski, Matic Lubej, Devis Peresutti, Jernej Puc, Tomislav Slijepčević (Sinergise)
Copyright (c) 2017-2019 Blaž Sovdat, Jovan Višnjić, Anže Zupanc, Lojze Žust (Sinergise)

This source code is licensed under the MIT license found in the LICENSE
file in the root directory of this source tree.
"""

import os
import re
import joblib
import logging

import numpy as np
import cv2
from skimage.morphology import disk

from sentinelhub import WmsRequest, WcsRequest, DataSource, CustomUrlParam, MimeType, ServiceType
from s2cloudless import S2PixelCloudDetector, MODEL_EVALSCRIPT

from eolearn.core import EOPatch, EOTask, get_common_timestamps
from .utilities import resize_images, map_over_axis


INTERP_METHODS = ['nearest', 'linear']

LOGGER = logging.getLogger(__name__)


class AddCloudMaskTask(EOTask):
    """ Task to add a cloud mask and cloud probability map to an EOPatch

    This task computes a cloud probability map and corresponding cloud binary mask for the input EOPatch. The classifier
    to be used to compute such maps must be provided at declaration. The `data_feature` to be used as input to the
    classifier is also a mandatory argument. If `data_feature` exists already, downscaling to the given (lower) cloud
    mask resolution is performed, the classifier is run, and upsampling returns the cloud maps to the original
    resolution.
    Otherwise, if `data_feature` does not exist, a new OGC request at the given cloud mask resolution is made, the
    classifier is run, and upsampling returns the cloud masks to original resolution. This design should allow faster
    execution of the classifier, and reduce the number of requests. `linear` interpolation is used for resampling of
    the `data_feature` and cloud probability map, while `nearest` interpolation is used to upsample the binary cloud
    mask.

    This implementation should allow usage with any cloud detector implemented for different data sources (S2, L8, ..).
    """
    def __init__(self, classifier, data_feature, cm_size_x=None, cm_size_y=None, cmask_feature='CLM',
                 cprobs_feature=None, instance_id=None, data_source=DataSource.SENTINEL2_L1C,
                 image_format=MimeType.TIFF_d32f, model_evalscript=MODEL_EVALSCRIPT):
        """ Constructor

        If both `cm_size_x` and `cm_size_y` are `None` and `data_feature` exists, cloud detection is computed at same
        resolution of `data_feature`.

        :param classifier: Cloud detector classifier. This object implements a `get_cloud_probability_map` and
                            `get_cloud_masks` functions to generate probability maps and binary masks
        :param data_feature: Name of key in eopatch.data dictionary to be used as input to the classifier. If the
                           `data_feature` does not exist, a new OGC request at the given cloud mask resolution is made
                           with layer name set to `data_feature` parameter.
        :param cm_size_x: Resolution to be used for computation of cloud mask. Allowed values are number of column
                            pixels (WMS-request) or spatial resolution (WCS-request, e.g. '10m'). Default is `None`
        :param cm_size_y: Resolution to be used for computation of cloud mask. Allowed values are number of row
                            pixels (WMS-request) or spatial resolution (WCS-request, e.g. '10m'). Default is `None`
        :param cmask_feature: Name of key to be used for the cloud mask to add. The cloud binary mask is added to the
                            `eopatch.mask` attribute dictionary. Default is `'clm'`.
        :param cprobs_feature: Name of key to be used for the cloud probability map to add. The cloud probability map is
                            added to the `eopatch.data` attribute dictionary. Default is `None`, so no cloud
                            probability map will be computed.
        :param instance_id: Instance ID to be used for OGC request. Default is `None`
        :param data_source: Data source to be requested by OGC service request. Default is `DataSource.SENTINEL2_L1C`
        :param image_format: Image format to be requested by OGC service request. Default is `MimeType.TIFF_d32f`
        :param model_evalscript: CustomUrlParam defining the EVALSCRIPT to be used by OGC request. Should reflect the
                            request necessary for the correct functioning of the classifier. For instance, for the
                            `S2PixelCloudDetector` classifier, `MODEL_EVALSCRIPT` is used as it requests the required 10
                            bands. Default is `MODEL_EVALSCRIPT`
        """
        self.classifier = classifier
        self.data_feature = data_feature
        self.cm_feature = cmask_feature
        self.cm_size_x = cm_size_x
        self.cm_size_y = cm_size_y
        self.cprobs_feature = cprobs_feature
        self.instance_id = instance_id
        self.data_source = data_source
        self.image_format = image_format
        self.model_evalscript = model_evalscript

    def _get_wms_request(self, bbox, time_interval, size_x, size_y, maxcc, time_difference, custom_url_params):
        """
        Returns WMS request.
        """
        return WmsRequest(layer=self.data_feature,
                          bbox=bbox,
                          time=time_interval,
                          width=size_x,
                          height=size_y,
                          maxcc=maxcc,
                          custom_url_params=custom_url_params,
                          time_difference=time_difference,
                          image_format=self.image_format,
                          data_source=self.data_source,
                          instance_id=self.instance_id)

    def _get_wcs_request(self, bbox, time_interval, size_x, size_y, maxcc, time_difference, custom_url_params):
        """
        Returns WCS request.
        """
        return WcsRequest(layer=self.data_feature,
                          bbox=bbox,
                          time=time_interval,
                          resx=size_x, resy=size_y,
                          maxcc=maxcc,
                          custom_url_params=custom_url_params,
                          time_difference=time_difference,
                          image_format=self.image_format,
                          data_source=self.data_source,
                          instance_id=self.instance_id)

    def _get_rescale_factors(self, reference_shape, meta_info):
        """ Compute the resampling factor for height and width of the input array

        :param reference_shape: Tuple specifying height and width in pixels of high-resolution array
        :type reference_shape: tuple of ints
        :param meta_info: Meta-info dictionary of input eopatch. Defines OGC request and parameters used to create the
                            eopatch
        :return: Rescale factor for rows and columns
        :rtype: tuple of floats
        """
        # Figure out resampling size
        height, width = reference_shape

        service_type = ServiceType(meta_info['service_type'])
        rescale = None
        if service_type == ServiceType.WMS:

            if (self.cm_size_x is None) and (self.cm_size_y is not None):
                rescale = (self.cm_size_y / height, self.cm_size_y / height)
            elif (self.cm_size_x is not None) and (self.cm_size_y is None):
                rescale = (self.cm_size_x / width, self.cm_size_x / width)
            else:
                rescale = (self.cm_size_y / height, self.cm_size_x / width)

        elif service_type == ServiceType.WCS:
            # Case where only one resolution for cloud masks is specified in WCS
            if self.cm_size_y is None:
                self.cm_size_y = self.cm_size_x
            elif self.cm_size_x is None:
                self.cm_size_x = self.cm_size_y

            hr_res_x, hr_res_y = int(meta_info['size_x'].strip('m')), int(meta_info['size_y'].strip('m'))
            lr_res_x, lr_res_y = int(self.cm_size_x.strip('m')), int(self.cm_size_y.strip('m'))
            rescale = (hr_res_y / lr_res_y, hr_res_x / lr_res_x)

        return rescale

    def _downscaling(self, hr_array, meta_info, interp='linear', smooth=True):
        """ Downscale existing array to resolution requested by cloud detector

        :param hr_array: High-resolution data array to be downscaled
        :param meta_info: Meta-info of eopatch
        :param interp: Interpolation method to be used in downscaling. Default is `'linear'`
        :param smooth: Apply Gaussian smoothing in spatial directions before downscaling. Sigma of kernel is estimated
                        by rescaling factor. Default is `True`
        :return: Down-scaled array
        """
        # Run cloud mask on full resolution
        if (self.cm_size_y is None) and (self.cm_size_x is None):
            return hr_array, None

        # Rescaling factor in spatial (width, height) dimensions
        rescale = self._get_rescale_factors(hr_array.shape[1:3], meta_info)

        lr_array = resize_images(hr_array,
                                 scale_factors=rescale,
                                 anti_alias=smooth,
                                 interpolation=interp)

        return lr_array, rescale

    @staticmethod
    def _upsampling(lr_array, rescale, reference_shape, interp='linear'):
        """ Upsample the low-resolution array to the original high-resolution grid

        :param lr_array: Low-resolution array to be upsampled
        :param rescale: Rescale factor for rows/columns
        :param reference_shape: Original size of high-resolution eopatch. Tuple with dimension for time, height and
                                width
        :param interp: Interpolation method ot be used in upsampling. Default is `'linear'`
        :return: Upsampled array. The array has 4 dimensions, the last one being of size 1
        """
        lr_shape = lr_array.shape + (1,)

        if rescale is None:
            return lr_array.reshape(lr_shape)

        # Resize to reference shape (height, width)
        output_size = reference_shape[1:3]
        hr_array = resize_images(lr_array.reshape(lr_shape),
                                 new_size=output_size,
                                 interpolation=interp)

        return hr_array

    def _make_request(self, bbox, meta_info, timestamps):
        """ Make OGC request to create input for cloud detector classifier

        :param bbox: Bounding box
        :param meta_info: Meta-info dictionary of input eopatch
        :return: Requested data
        """
        service_type = ServiceType(meta_info['service_type'])

        # Raise error if resolutions are not specified
        if self.cm_size_x is None and self.cm_size_y is None:
            raise ValueError("Specify size_x and size_y for data request")

        # If WCS request, make sure both resolutions are set
        if service_type == ServiceType.WCS:
            if self.cm_size_y is None:
                self.cm_size_y = self.cm_size_x
            elif self.cm_size_x is None:
                self.cm_size_x = self.cm_size_y

        custom_url_params = {CustomUrlParam.SHOWLOGO: False,
                             CustomUrlParam.TRANSPARENT: False,
                             CustomUrlParam.EVALSCRIPT: self.model_evalscript}

        request = {ServiceType.WMS: self._get_wms_request,
                   ServiceType.WCS: self._get_wcs_request}[service_type](bbox,
                                                                         meta_info['time_interval'],
                                                                         self.cm_size_x,
                                                                         self.cm_size_y,
                                                                         meta_info['maxcc'],
                                                                         meta_info['time_difference'],
                                                                         custom_url_params)

        request_dates = request.get_dates()
        download_frames = get_common_timestamps(request_dates, timestamps)

        request_return = request.get_data(raise_download_errors=False, data_filter=download_frames)
        bad_data = [idx for idx, value in enumerate(request_return) if value is None]
        for idx in reversed(sorted(bad_data)):
            LOGGER.warning('Data from %s could not be downloaded for %s!', str(request_dates[idx]), self.data_feature)
            del request_return[idx]
            del request_dates[idx]

        return np.asarray(request_return), request_dates

    def execute(self, eopatch):
        """ Add cloud binary mask and (optionally) cloud probability map to input eopatch

        :param eopatch: Input `EOPatch` instance
        :return: `EOPatch` with additional cloud maps
        """
        # Downsample or make request
        if not eopatch.data:
            raise ValueError('EOPatch must contain some data feature')
        if self.data_feature in eopatch.data:
            new_data, rescale = self._downscaling(eopatch.data[self.data_feature], eopatch.meta_info)
            reference_shape = eopatch.data[self.data_feature].shape[:3]
        else:
            new_data, new_dates = self._make_request(eopatch.bbox, eopatch.meta_info, eopatch.timestamp)
            removed_frames = eopatch.consolidate_timestamps(new_dates)
            for rm_frame in removed_frames:
                LOGGER.warning('Removed data for frame %s from '
                               'eopatch due to unavailability of %s!', str(rm_frame), self.data_feature)

            # Get reference shape from first item in data dictionary
            if not eopatch.data:
                raise ValueError('Given EOPatch does not have any data feature')

            reference_data_feature = sorted(eopatch.data)[0]
            reference_shape = eopatch.data[reference_data_feature].shape[:3]
            rescale = self._get_rescale_factors(reference_shape[1:3], eopatch.meta_info)

        clf_probs_lr = self.classifier.get_cloud_probability_maps(new_data)
        clf_mask_lr = self.classifier.get_mask_from_prob(clf_probs_lr)

        # Add cloud mask as a feature to EOPatch
        clf_mask_hr = self._upsampling(clf_mask_lr, rescale, reference_shape, interp='nearest')
        eopatch.mask[self.cm_feature] = clf_mask_hr.astype(np.bool)

        # If the feature name for cloud probability maps is specified, add as feature
        if self.cprobs_feature is not None:
            clf_probs_hr = self._upsampling(clf_probs_lr, rescale, reference_shape, interp='linear')
            eopatch.data[self.cprobs_feature] = clf_probs_hr.astype(np.float32)

        return eopatch


def get_s2_pixel_cloud_detector(threshold=0.4, average_over=4, dilation_size=2, all_bands=True):
    """ Wrapper function for pixel-based S2 cloud detector `S2PixelCloudDetector`
    """
    return S2PixelCloudDetector(threshold=threshold,
                                average_over=average_over,
                                dilation_size=dilation_size,
                                all_bands=all_bands)


# Twin classifier
MONO_CLASSIFIER_NAME = 'pixel_s2_cloud_detector_lightGBM_v0.1.joblib.dat'
MULTI_CLASSIFIER_NAME = 'ssim_s2_cloud_detector_lightGBM_v0.2.joblib.dat'

class AddMultiCloudMaskTask(EOTask):
    """ This task wraps around s2cloudless and the SSIM-based multi-temporal classifier.
    Its intended output is a cloud mask that is based on the outputs of both
    individual classifiers (a dilated intersection of individual binary masks).
    Additional cloud masks and probabilities can be added for either classifier or both.

    The task computes cloud probabilities and binary masks

    Prior to feature extraction and classification, it is recommended that the input be
    downscaled by specifying the source and processing resolutions. This should be done
    for the following reasons:
        - faster execution
        - lower memory consumption
        - noise mitigation

    Resizing is performed with linear interpolation. After classification, the cloud
    probabilities are themselves upscaled to the original dimensions, before proceeding
    with masking operations.
    """

    def __init__(self,
                 mono_classifier=None,
                 multi_classifier=None,
                 data_feature='BANDS-S2-L1C',
                 is_data_feature='IS_DATA',
                 all_bands=True,
                 src_res=None,
                 proc_res=None,
                 max_proc_frames=11,
                 mono_proba_feature=None,
                 multi_proba_feature=None,
                 mono_mask_feature=None,
                 multi_mask_feature=None,
                 mask_feature='CLM_INTERSSIM',
                 mono_threshold=0.4,
                 multi_threshold=0.5,
                 average_over=1,
                 dilation_size=1
                ):

        # Load classifiers
        if mono_classifier is None or multi_classifier is None:
            classifier_dir = os.path.dirname(__file__)

            if mono_classifier is None:
                mono_classifier = joblib.load(os.path.join(classifier_dir, 'models', MONO_CLASSIFIER_NAME))

            if multi_classifier is None:
                multi_classifier = joblib.load(os.path.join(classifier_dir, 'models', MULTI_CLASSIFIER_NAME))

        self.mono_classifier = mono_classifier
        self.multi_classifier = multi_classifier

        # Set data info
        self.data_feature = data_feature
        self.is_data_feature = is_data_feature
        self.band_indices = (0,1,3,4,7,8,9,10,11,12) if all_bands else tuple(range(10))

        # If only resolution of the source is specified, the sigma alone can be adjusted
        if src_res is not None and proc_res is None:

            src_res_ = src_res if type(src_res) == int else int(re.match('\d+', src_res).group())

            self.sigma = 100. / src_res_
            self.scale_factors = None

        # If both resolution of the source and mid-product is specified, resizing is taken into account
        elif src_res is not None and proc_res is not None:

            src_res_ = src_res if type(src_res) == int else int(re.match('\d+', src_res).group())
            proc_res_ = proc_res if type(proc_res) == int else int(re.match('\d+', proc_res).group())

            self.sigma = 100. / proc_res_
            self.scale_factors = (src_res_ / proc_res_,)*2

        # In any other case, no resizing is performed and sigma is set to a non-volatile level
        else:

            self.sigma = 1.0
            self.scale_factors = None

        # Set max frames for single iteration
        self.max_proc_frames = max_proc_frames

        # Set feature info
        self.mono_proba_feature = mono_proba_feature
        self.multi_proba_feature = multi_proba_feature
        self.mono_mask_feature = mono_mask_feature
        self.multi_mask_feature = multi_mask_feature
        self.mask_feature = mask_feature

        # Set thresholding and morph. ops. parameters and kernels
        self.mono_threshold = mono_threshold
        self.multi_threshold = multi_threshold

        if average_over is not None and average_over > 0:
            self.avg_kernel = disk(average_over) / np.sum(disk(average_over))
        else:
            self.avg_kernel = None

        if dilation_size is not None and dilation_size > 0:
            self.dil_kernel = disk(dilation_size).astype(np.uint8)
        else:
            self.dil_kernel = None

    @staticmethod
    def _get_max(x):
        return np.ma.max(x, axis=0).data

    @staticmethod
    def _get_min(x):
        return np.ma.min(x, axis=0).data

    @staticmethod
    def _get_mean(x):
        return np.ma.mean(x, axis=0).data

    @staticmethod
    def _get_std(x):
        return np.ma.std(x, axis=0).data

    def _frame_indices(self, num_of_frames, target_idx):
        """
        Returns frame indices within a given time window, with the target index relative to it.
        """

        # Get reach
        nt_min = target_idx - self.max_proc_frames//2
        nt_max = target_idx + self.max_proc_frames - self.max_proc_frames//2

        # Shift reach
        shift = max(0, -nt_min) - max(0, nt_max-num_of_frames)
        nt_min += shift
        nt_max += shift

        # Get indices within range
        nt_min = max(0, nt_min)
        nt_max = min(num_of_frames, nt_max)
        nt_rel = target_idx - nt_min

        return nt_min, nt_max, nt_rel

    def _red_ssim(self, x, y, valid_mask, mu1, mu2, sigma1_2, sigma2_2, c1=1e-6, c2=1e-5):
        """
        Slightly reduced (pre-computed) SSIM computation.
        """

        # Increase precision and mask invalid regions
        valid_mask = valid_mask.astype(np.float64)
        x = x.astype(np.float64) * valid_mask
        y = y.astype(np.float64) * valid_mask

        # Init
        mu1_2 = mu1 * mu1
        mu2_2 = mu2 * mu2
        mu1_mu2 = mu1 * mu2

        sigma12 = cv2.GaussianBlur((x*y).astype(np.float64), (0,0), self.sigma, borderType=cv2.BORDER_REFLECT)
        sigma12 -= mu1_mu2

        # Formula
        tmp1 = 2. * mu1_mu2 + c1
        tmp2 = 2. * sigma12 + c2
        num = tmp1 * tmp2

        tmp1 = mu1_2 + mu2_2 + c1
        tmp2 = sigma1_2 + sigma2_2 + c2
        den = tmp1 * tmp2

        return np.divide(num, den)

    def _win_avg(self, x):
        """
        Spatial window average.
        """

        return cv2.GaussianBlur(x.astype(np.float64), (0,0), self.sigma, borderType=cv2.BORDER_REFLECT)

    def _win_prevar(self, x):
        """
        Incomplete spatial window variance.
        """

        return cv2.GaussianBlur((x*x).astype(np.float64), (0,0), self.sigma, borderType=cv2.BORDER_REFLECT)

    def _resize(self, x):
        downscaling = self.scale_factors[0] < 1 or self.scale_factors[0] < 1
        old_size = (x.shape[1], x.shape[0])
        new_size = tuple([int(d * f) for d,f in zip(old_size, self.scale_factors)])

        # Perform anti-alias smoothing if downscaling
        if downscaling:
            sx, sy = [((1/s) - 1)/2 for s in self.scale_factors]
            x = cv2.GaussianBlur(x, (0,0), sigmaX=sx, sigmaY=sy, borderType=cv2.BORDER_REFLECT)

        return cv2.resize(x, new_size, interpolation=cv2.INTER_LINEAR)

    def _average(self, x):
        return cv2.filter2D(x.astype(np.float64), -1, self.avg_kernel, borderType=cv2.BORDER_REFLECT)

    def _dilate(self, x):
        return (cv2.dilate(x.astype(np.uint8), self.dil_kernel) > 0).astype(np.uint8)

    @staticmethod
    def _map_sequence(data, func2d):
        """
        Iterate over time and band dimensions and apply a function to each slice.
        Returns a new array with the combined results.

        :param data: input array
        :type data: array of shape (timestamps, rows, columns, channels)
        :param func2d: Mapping function that is applied on each 2d image slice. All outputs must have the same shape.
        :type func2d: function (rows, columns) -> (new_rows, new_columns)
        """

        func3d = lambda x: map_over_axis(x, func2d, axis=2) # Map over channel dimension on 3d tensor
        func4d = lambda x: map_over_axis(x, func3d, axis=0) # Map over time dimension on 4d tensor

        output = func4d(data)

        return output

    def _average_all(self, data):
        if self.avg_kernel is not None:
            return self._map_sequence(data, self._average)
        else:
            return data

    def _dilate_all(self, data):
        if self.dil_kernel is not None:
            return self._map_sequence(data, self._dilate)
        else:
            return data

    def _ssim_stats(self, bands, is_data, mu, var, nt_rel):

        ssim_max = np.empty((1, *bands.shape[1:]), dtype=np.float32)
        ssim_mean = np.empty_like(ssim_max)
        ssim_std = np.empty_like(ssim_max)

        bands_r = np.delete(bands, nt_rel, axis=0)
        mu_r = np.delete(mu, nt_rel, axis=0)
        var_r = np.delete(var, nt_rel, axis=0)

        n_frames = bands_r.shape[0]
        n_bands = bands_r.shape[-1]

        valid_mask = np.delete(is_data, nt_rel, axis=0) & is_data[nt_rel,...,0].reshape(1,*is_data.shape[1:-1],1)

        for b_i in range(n_bands):
            local_ssim = []

            for t_j in range(n_frames):
                ssim_ij = self._red_ssim(bands[nt_rel,...,b_i],
                                         bands_r[t_j,...,b_i],
                                         valid_mask[t_j,...,0],
                                         mu[nt_rel,...,b_i],
                                         mu_r[t_j,...,b_i],
                                         var[nt_rel,...,b_i],
                                         var_r[t_j,...,b_i]
                                        )

                local_ssim.append(ssim_ij)

            local_ssim = np.ma.array(np.stack(local_ssim), mask=~valid_mask)

            ssim_max[0,...,b_i] = self._get_max(local_ssim)
            ssim_mean[0,...,b_i] = self._get_mean(local_ssim)
            ssim_std[0,...,b_i] = self._get_std(local_ssim)

        return ssim_max, ssim_mean, ssim_std

    def _mono_iterations(self, bands):

        # Init
        mono_proba = np.empty((np.prod(bands.shape[:-1]),1))
        img_size = np.prod(bands.shape[1:-1])

        t = bands.shape[0]

        for t_i in range(0, t, self.max_proc_frames):

            # Extract mono features
            nt_min = t_i
            nt_max = min(t_i+self.max_proc_frames, t)

            bands_t = bands[nt_min:nt_max]

            mono_features = bands_t.reshape(np.prod(bands_t.shape[:-1]), bands_t.shape[-1])

            # Run mono classifier
            mono_proba[nt_min*img_size:nt_max*img_size] = self.mono_classifier.predict_proba(mono_features)[...,1:]

        return mono_proba

    def _multi_iterations(self, bands, is_data):

        # Init
        multi_proba = np.empty((np.prod(bands.shape[:-1]),1))
        img_size = np.prod(bands.shape[1:-1])

        t = bands.shape[0]

        loc_mu = None
        loc_var = None

        prev_nt_min = None
        prev_nt_max = None
        prev_nt_rel = None

        for t_i in range(t):

            # Extract temporal window indices
            nt_min, nt_max, nt_rel = self._frame_indices(t, t_i)

            bands_t = bands[nt_min:nt_max]
            is_data_t = is_data[nt_min:nt_max]

            bands_i = bands_t[nt_rel][None,...]
            is_data_i = is_data_t[nt_rel][None,...]

            masked_bands = np.ma.array(bands_t, mask=~is_data_t.repeat(bands_t.shape[-1],axis=-1))

            # Add window averages and variances to local data
            if loc_mu is None:
                win_avg_bands = self._map_sequence(bands_t, self._win_avg)
                win_avg_is_data = self._map_sequence(is_data_t, self._win_avg)

                win_avg_is_data[win_avg_is_data == 0.] = 1.
                # win_avg_is_data[~is_data_t] = 1.
                true_win_avg = win_avg_bands / win_avg_is_data

                loc_mu = true_win_avg

                win_prevars = self._map_sequence(bands_t, self._win_prevar)
                win_prevars -= loc_mu*loc_mu

                loc_var = win_prevars

            elif prev_nt_min != nt_min or prev_nt_max != nt_max:

                win_avg_bands = self._map_sequence(bands_t[-1][None,...], self._win_avg)
                win_avg_is_data = self._map_sequence(is_data_t[-1][None,...], self._win_avg)

                win_avg_is_data[win_avg_is_data == 0.] = 1.
                # win_avg_is_data[~is_data_t[-1][None,...]] == 1.
                true_win_avg = win_avg_bands / win_avg_is_data

                loc_mu[:-1] = loc_mu[1:]
                loc_mu[-1] = true_win_avg[0]

                win_prevars = self._map_sequence(bands_t[-1][None,...], self._win_prevar)
                win_prevars[0] -= loc_mu[-1]*loc_mu[-1]

                loc_var[:-1] = loc_var[1:]
                loc_var[-1] = win_prevars[0]

            # Compute SSIM stats
            ssim_max, ssim_mean, ssim_std = self._ssim_stats(bands_t, is_data_t, loc_mu, loc_var, nt_rel)

            ssim_interweaved = np.empty((*ssim_max.shape[:-1], 3*ssim_max.shape[-1]))
            ssim_interweaved[...,0::3] = ssim_max
            ssim_interweaved[...,1::3] = ssim_mean
            ssim_interweaved[...,2::3] = ssim_std

            # Compute temporal stats
            temp_min = self._get_min(masked_bands)[None,...]
            temp_mean = self._get_mean(masked_bands)[None,...]

            temp_interweaved = np.empty((*temp_min.shape[:-1], 2*temp_min.shape[-1]))
            temp_interweaved[...,0::2] = temp_min
            temp_interweaved[...,1::2] = temp_mean

            # Compute difference stats
            t_all = len(bands_t)
            t_rest = t_all-1

            diff_max = (masked_bands[nt_rel][None,...] - temp_min).data
            diff_mean = (masked_bands[nt_rel][None,...]*(1. + 1./t_rest) - t_all*temp_mean/t_rest).data

            diff_interweaved = np.empty((*diff_max.shape[:-1], 2*diff_max.shape[-1]))
            diff_interweaved[...,0::2] = diff_max
            diff_interweaved[...,1::2] = diff_mean

            # Put it all together
            multi_features = np.concatenate((bands_i,
                                             loc_mu[nt_rel][None,...],
                                             ssim_interweaved,
                                             temp_interweaved,
                                             diff_interweaved
                                            ),
                                            axis=3
                                           )

            multi_features = multi_features.reshape(np.prod(multi_features.shape[:-1]), multi_features.shape[-1])

            # Run multi classifier
            multi_proba[t_i*img_size:(t_i+1)*img_size] = self.multi_classifier.predict_proba(multi_features)[...,1:]

            prev_nt_min = nt_min
            prev_nt_max = nt_max
            prev_nt_rel = nt_rel

        return multi_proba

    def execute(self, eopatch):
        """
        Add optional features (cloud probabilities and masks) to an EOPatch instance.

        :param eopatch: Input `EOPatch` instance
        :return: `EOPatch` with additional features
        """

        # Get data
        bands = eopatch.data[self.data_feature][...,self.band_indices].astype(np.float32)
        is_data = eopatch.mask[self.is_data_feature].astype(bool)

        # Downscale if specified
        if self.scale_factors is not None:
            original_shape = bands.shape[1:-1]

            bands = resize_images(bands.astype(np.float32), scale_factors=self.scale_factors)
            is_data = resize_images(is_data.astype(np.uint8), scale_factors=self.scale_factors).astype(np.bool)

        new_shape = bands.shape[1:-1]

        # Use only s2cloudless
        if self.multi_proba_feature is None and self.multi_mask_feature is None and self.mask_feature is None:

            # Run mono extraction and classification iters
            mono_proba = self._mono_iterations(bands)
            multi_proba = None

        # Use only the SSIM-based multi-temporal classifier
        elif self.mono_proba_feature is None and self.mono_mask_feature is None and self.mask_feature is None:

            # Run multi extraction and classification iters
            multi_proba = self._multi_iterations(bands, is_data)
            mono_proba = None

        # Otherwise, use InterSSIM
        else:

            # Run multi extraction and classification iters
            multi_proba = self._multi_iterations(bands, is_data)

            # Run mono extraction and classification iters
            mono_proba = self._mono_iterations(bands)

        # Reshape
        if mono_proba is not None:
            mono_proba = mono_proba.reshape(*bands.shape[:-1], 1)

        if multi_proba is not None:
            multi_proba = multi_proba.reshape(*bands.shape[:-1], 1)

        # Upscale (rescale) if specified
        if self.scale_factors is not None:
            if mono_proba is not None:
                mono_proba = resize_images(mono_proba, new_size=original_shape)

            if multi_proba is not None:
                multi_proba = resize_images(multi_proba, new_size=original_shape)

        # Average over and threshold
        if self.mono_mask_feature is not None or self.mask_feature is not None:
            mono_mask = self._average_all(mono_proba) >= self.mono_threshold

        if self.multi_mask_feature is not None or self.mask_feature is not None:
            multi_mask = self._average_all(multi_proba) >= self.multi_threshold

        # Intersect
        if self.mask_feature is not None:
            inter_mask = mono_mask & multi_mask

        # Add features
        is_data = eopatch.mask[self.is_data_feature].astype(bool)

        if self.mono_mask_feature is not None:
            mono_mask = self._dilate_all(mono_mask)
            eopatch.mask[self.mono_mask_feature] = (mono_mask * is_data).astype(bool)

        if self.multi_mask_feature is not None:
            multi_mask = self._dilate_all(multi_mask)
            eopatch.mask[self.multi_mask_feature] = (multi_mask * is_data).astype(bool)

        if self.mask_feature is not None:
            inter_mask = self._dilate_all(inter_mask)
            eopatch.mask[self.mask_feature] = (inter_mask * is_data).astype(bool)

        if self.mono_proba_feature is not None:
            eopatch.data[self.mono_proba_feature] = (mono_proba * is_data).astype(np.float32)

        if self.multi_proba_feature is not None:
            eopatch.data[self.multi_proba_feature] = (multi_proba * is_data).astype(np.float32)

        return eopatch
