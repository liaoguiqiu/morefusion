import chainer
from chainer.backends import cuda
import chainer.functions as F
import chainer.links as L
import numpy as np

import objslampp

from .pspnet import PSPNetExtractor


class BaselineModel(chainer.Chain):

    _models = objslampp.datasets.YCBVideoModels()
    _voxel_dim = 32

    def __init__(
        self,
        *,
        n_fg_class,
        loss=None,
        loss_scale=None,
    ):
        super().__init__()

        self._n_fg_class = n_fg_class

        self._loss = 'add/add_s' if loss is None else loss
        assert self._loss in [
            'add',
            'add_s',
            'add/add_s',
            'add+add_s',
        ]

        if loss_scale is None:
            loss_scale = {
                'add+add_s': 0.5,
            }
        self._loss_scale = loss_scale

        with self.init_scope():
            # extractor
            self.resnet_extractor = objslampp.models.ResNet18()
            self.pspnet_extractor = PSPNetExtractor()
            self.voxel_extractor = VoxelFeatureExtractor()

            self.fc1_rot = L.Linear(1024, 640, 1)
            self.fc1_trans = L.Linear(1024, 640, 1)
            self.fc2_rot = L.Linear(640, 256, 1)
            self.fc2_trans = L.Linear(640, 256, 1)
            self.fc3_rot = L.Linear(256, 128, 1)
            self.fc3_trans = L.Linear(256, 128, 1)
            self.fc4_rot = L.Linear(128, n_fg_class * 4, 1)
            self.fc4_trans = L.Linear(128, n_fg_class * 3, 1)

    def predict(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
    ):
        values, points = self._extract(
            rgb=rgb,
            pcd=pcd,
        )

        h, actives = self._voxelize(
            class_id=class_id,
            pitch=pitch,
            origin=origin,
            values=values,
            points=points,
        )

        return self._predict_from_voxel(
            class_id=class_id,
            pitch=pitch,
            origin=origin,
            matrix=h,
            actives=actives,
        )

    def _extract(self, rgb, pcd):
        xp = self.xp

        B, H, W, C = rgb.shape
        assert H == W == 256
        assert C == 3

        # prepare
        rgb = rgb.transpose(0, 3, 1, 2).astype(np.float32)  # BHWC -> BCHW
        pcd = pcd.transpose(0, 3, 1, 2).astype(np.float32)  # BHW3 -> B3HW

        # feature extraction
        mean = xp.asarray(self.resnet_extractor.mean)
        h_rgb = self.resnet_extractor(rgb - mean[None])  # 1/8
        h_rgb = self.pspnet_extractor(h_rgb)  # 1/1

        mask = ~xp.isnan(pcd).any(axis=1)  # BHW
        values = []  # BMC
        points = []  # BM3
        for i in range(B):
            # mask[i]:  HW
            # h_rgb[i]: CHW
            # pcd[i]:   3HW
            values.append(h_rgb[i][:, mask[i]].transpose(1, 0))  # MC
            points.append(pcd[i][:, mask[i]].transpose(1, 0))    # M3

        return values, points

    def _voxelize(
        self,
        class_id,
        pitch,
        origin,
        values,
        points,
    ):
        xp = self.xp

        B = class_id.shape[0]
        dimensions = (self._voxel_dim,) * 3

        # prepare
        pitch = pitch.astype(np.float32)
        origin = origin.astype(np.float32)

        h = []
        actives = []
        for i in range(B):
            h_i, counts_i = objslampp.functions.average_voxelization_3d(
                values=values[i],
                points=points[i],
                origin=origin[i],
                pitch=pitch[i],
                dimensions=dimensions,
                channels=values[i].shape[1],
                return_counts=True,
            )  # CXYZ
            actives_i = counts_i[0] > 0

            h.append(h_i[None])
            actives.append(actives_i[None])

        h = F.concat(h, axis=0)           # BCXYZ
        actives = xp.concatenate(actives, axis=0)  # BXYZ

        return h, actives

    def _predict_from_voxel(
        self, class_id, pitch, origin, matrix, actives
    ):
        xp = self.xp
        B = class_id.shape[0]

        centroids = []
        for i in range(B):
            # mean of active points (voxels)
            centroid = xp.stack(xp.where(actives[i])).mean(axis=1)
            centroid = centroid * pitch[i] + origin[i]
            centroids.append(centroid[None])
        centroids = xp.concatenate(centroids, axis=0)  # B3

        h = self.voxel_extractor(matrix, actives)

        h_rot = F.relu(self.fc1_rot(h))
        h_trans = F.relu(self.fc1_trans(h))
        h_rot = F.relu(self.fc2_rot(h_rot))
        h_trans = F.relu(self.fc2_trans(h_trans))
        h_rot = F.relu(self.fc3_rot(h_rot))
        h_trans = F.relu(self.fc3_trans(h_trans))
        cls_rot = self.fc4_rot(h_rot)
        cls_trans = self.fc4_trans(h_trans)

        quaternion = cls_rot.reshape(B, self._n_fg_class, 4)
        translation = cls_trans.reshape(B, self._n_fg_class, 3)

        fg_class_id = class_id - 1
        quaternion = quaternion[xp.arange(B), fg_class_id, :]
        translation = translation[xp.arange(B), fg_class_id, :]

        quaternion = F.normalize(quaternion, axis=1)
        translation = centroids + translation * pitch[:, None]

        return quaternion, translation

    def __call__(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
        quaternion_true,
        translation_true,
    ):
        keep = class_id != -1
        if keep.sum() == 0:
            return chainer.Variable(self.xp.zeros((), dtype=np.float32))

        class_id = class_id[keep]
        pitch = pitch[keep]
        origin = origin[keep]
        rgb = rgb[keep]
        pcd = pcd[keep]
        quaternion_true = quaternion_true[keep]
        translation_true = translation_true[keep]

        quaternion_pred, translation_pred = self.predict(
            class_id=class_id,
            pitch=pitch,
            origin=origin,
            rgb=rgb,
            pcd=pcd,
        )

        self.evaluate(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )

        loss = self.loss(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )
        return loss

    def evaluate(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        quaternion_true = quaternion_true.astype(np.float32)
        translation_true = translation_true.astype(np.float32)

        B = class_id.shape[0]

        T_cad2cam_true = objslampp.functions.transformation_matrix(
            quaternion_true, translation_true
        )
        T_cad2cam_pred = objslampp.functions.transformation_matrix(
            quaternion_pred, translation_pred
        )
        T_cad2cam_true = cuda.to_cpu(T_cad2cam_true.array)
        T_cad2cam_pred = cuda.to_cpu(T_cad2cam_pred.array)

        summary = chainer.DictSummary()
        for i in range(B):
            class_id_i = int(class_id[i])
            cad_pcd = self._models.get_pcd(class_id=class_id_i)
            add, add_s = objslampp.metrics.average_distance(
                [cad_pcd], [T_cad2cam_true[i]], [T_cad2cam_pred[i]]
            )
            add, add_s = add[0], add_s[0]
            if chainer.config.train:
                summary.add({'add': add, 'add_s': add_s})
            else:
                summary.add({
                    f'add/{class_id_i:04d}': add,
                    f'add_s/{class_id_i:04d}': add_s,
                })
        chainer.report(summary.compute_mean(), self)

    def loss(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        quaternion_true = quaternion_true.astype(np.float32)
        translation_true = translation_true.astype(np.float32)

        T_cad2cam_true = objslampp.functions.transformation_matrix(
            quaternion_true, translation_true
        )
        T_cad2cam_pred = objslampp.functions.transformation_matrix(
            quaternion_pred, translation_pred
        )

        B = class_id.shape[0]

        loss = 0
        for i in range(B):
            class_id_i = int(class_id[i])

            if self._loss in [
                'add',
                'add_s',
                'add/add_s',
                'add+add_s',
            ]:
                if self._loss in ['add+add_s']:
                    is_symmetric = None
                elif self._loss in ['add']:
                    is_symmetric = False
                elif self._loss in ['add_s']:
                    is_symmetric = True
                else:
                    assert self._loss in ['add/add_s']
                    is_symmetric = class_id_i in \
                        objslampp.datasets.ycb_video.class_ids_symmetric
                cad_pcd = self._models.get_pcd(class_id=class_id_i)
                cad_pcd = self.xp.asarray(cad_pcd, dtype=np.float32)

            if self._loss in [
                'add',
                'add_s',
                'add/add_s',
            ]:
                assert is_symmetric in [True, False]
                loss_i = objslampp.functions.average_distance_l1(
                    points=cad_pcd,
                    transform1=T_cad2cam_true[i][None],
                    transform2=T_cad2cam_pred[i][None],
                    symmetric=is_symmetric,
                )[0]
            elif self._loss in ['add+add_s']:
                kwargs = dict(
                    points=cad_pcd,
                    transform1=T_cad2cam_true[i][None],
                    transform2=T_cad2cam_pred[i][None],
                )
                loss_add_i = objslampp.functions.average_distance_l1(
                    **kwargs, symmetric=False
                )[0]
                loss_add_s_i = objslampp.functions.average_distance_l1(
                    **kwargs, symmetric=True
                )[0]
                loss_i = (
                    self._loss_scale['add+add_s'] * loss_add_i +
                    (1 - self._loss_scale['add+add_s']) * loss_add_s_i
                )
            else:
                raise ValueError(f'unsupported loss: {self._loss}')

            loss += loss_i
        loss /= B

        values = {'loss': loss}
        chainer.report(values, observer=self)

        return loss


class VoxelFeatureExtractor(chainer.Chain):

    def __init__(self):
        super().__init__()
        with self.init_scope():
            self.conv1 = L.Convolution3D(None, 128, 4, stride=2, pad=1)
            self.conv2 = L.Convolution3D(128, 256, 3, stride=1, pad=1)
            self.conv3 = L.Convolution3D(256, 512, 3, stride=1, pad=1)
            self.conv4 = L.Convolution3D(512, 1024, 3, stride=1, pad=1)

    def __call__(self, h, actives):
        xp = self.xp

        h = F.relu(self.conv1(h))
        h = F.relu(self.conv2(h))
        h = F.relu(self.conv3(h))
        h = F.relu(self.conv4(h))

        h_ = []
        for i in range(h.shape[0]):
            X, Y, Z = xp.where(actives[i, :, :, :])
            X, Y, Z = X // 2, Y // 2, Z // 2
            h_i = h[i, :, X, Y, Z]
            h_i = F.average(h_i, axis=0)
            h_.append(h_i[None])
        h = F.concat(h_, axis=0)
        del h_

        return h
