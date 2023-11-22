# --------------------------------------------------------
# Licensed under The MIT License [see LICENSE for details]
# --------------------------------------------------------

from . import _init_paths
from ycb_render.ycb_renderer import YCBRenderer # comment
from ycb_render.robotPose.robot_pykdl import *
from . import config

from .planner import Planner
from scipy.spatial import cKDTree
import time

from .util import *
from .sdf_tools import *
import torch


torch.no_grad()
import IPython


class Trajectory(object):
    """
    Trajectory class that wraps an object or an obstacle
    """

    def __init__(self, timesteps=100, dof=9):
        """
        Initialize fixed endpoint trajectory.
        """
        self.timesteps = config.cfg.timesteps
        self.dof = dof
        self.data = np.zeros([self.timesteps, dof])  # fixed start and end
        self.goal_set = []
        self.goal_quality = []

        self.start = np.array([0.0, -1.285, 0, -2.356, 0.0, 1.571, 0.785, 0.04, 0.04])
        self.end = np.array([-0.99, -1.74, -0.61, -3.04, 0.88, 1.21, -1.12, 0.04, 0.04])

        self.interpolate_waypoints(mode=config.cfg.traj_interpolate)

    def update(self, grad):
        """
        Update trajectory based on functional gradient.
        """
        if config.cfg.consider_finger:
            self.data += grad
        else:
            self.data[:, :-2] += grad[:, :-2]
        self.data[:, -2:] = np.minimum(np.maximum(self.data[:, -2:], 0), 0.04)

    def set(self, new_traj):
        """
        Set trajectory by given data.
        """
        self.data = new_traj

    def interpolate_waypoints(self, waypoints=None, mode="cubic"):  # linear
        """
        Interpolate the waypoints using interpolation.
        """
        timesteps = config.cfg.timesteps
        if config.cfg.dynamic_timestep:
            timesteps = min(
                max(
                    int(np.linalg.norm(self.start - self.end) / config.cfg.traj_delta),
                    config.cfg.traj_min_step,
                ),
                config.cfg.traj_max_step,
            )
            config.cfg.timesteps = timesteps
            self.data = np.zeros([timesteps, self.dof])  # fixed start and end
            config.cfg.get_global_param(timesteps)
            self.timesteps = timesteps
        self.data = interpolate_waypoints(
            np.stack([self.start, self.end]), timesteps, self.start.shape[0], mode=mode
        )


class Model(object):
    """
    Model class that wraps an object or an obstacle
    """

    def __init__(
        self, path=None, id=0, target=True, pose=None, compute_grasp=True, name=None
    ):
        path = config.cfg.root_dir + path
        self.mesh_path = path + "model_normalized.obj"
        self.pose_mat = np.eye(4)
        self.pose_mat[:3, 3] = [0.1, 0.04, 0.15]  # 0.15  [0.3, 0.04, 0.55]
        self.pose_mat[:3, :3] = euler2mat(-np.pi / 2, np.pi, np.pi)
        if pose is not None:
            self.pose_mat = pose

        self.pose = pack_pose(self.pose_mat)
        self.type = target
        self.model_name = path.split("/")[-2]
        self.id = id
        if name is None:
            self.name = self.model_name
        else:
            self.name = name
        try:
            self.extents = np.loadtxt(path + "model_normalized.extent.txt").astype(np.float32)
            self.resize  = config.cfg.target_size if target else config.cfg.obstacle_size
            self.sdf = SignedDensityField.from_pth(path + "model_normalized_chomp.pth")  #
            self.sdf.resize(config.cfg.target_size)
            self.sdf.data[self.sdf.data < 0] *= config.cfg.penalize_constant
        except:
            pass

        self.compute_grasp = compute_grasp
        self.grasps = []
        self.reach_grasps = []
        self.seeds = []
        self.grasp_potentials = []
        self.grasp_vis_points = []
        self.attached = False
        self.rel_hand_pose = None
        self.vis_points = None
        self.grasps_poses = []
        if self.name.startswith("0"):
            self.points = np.loadtxt(path + "model_normalized.xyz")
            self.points = self.points[random.sample(range(self.points.shape[0]), 500)]

    def world_to_obj(self, points):
        return np.swapaxes(
            (np.matmul(
                    self.pose_mat[:3, :3].T,
                    (np.swapaxes(points, -1, -2) - self.pose_mat[:3, [3]]),
                )), -1, -2, )

    def update_pose(self, pose):
        self.pose = pose
        self.pose_mat = unpack_pose(pose)


class Robot(object):
    """
    Robot class
    """

    def __init__(self, data_path):
        self.robot_kinematics = robot_kinematics("panda_arm", data_path=data_path)
        self.extents = np.loadtxt(config.cfg.robot_model_path + "/extents.txt")
        self.sphere_size = (
            np.linalg.norm(self.extents, axis=-1).reshape([self.extents.shape[0], 1])
            / 2.0
        )
        self.collision_points = self.load_collision_points()

        self.hand_col_points = self.collision_points[-3:].copy()
        self.joint_names = self.robot_kinematics._joint_name[:]
        del self.joint_names[-3]  # remove dummy hand finger joint
        self.joint_lower_limit = np.array(
            [[self.robot_kinematics._joint_limits[n][0] for n in self.joint_names]]
        )
        self.joint_upper_limit = np.array(
            [[self.robot_kinematics._joint_limits[n][1] for n in self.joint_names]]
        )
        self.joint_lower_limit[:, :-2] += config.cfg.soft_joint_limit_padding
        self.joint_upper_limit[:, :-2] -= config.cfg.soft_joint_limit_padding

    def load_collision_points(self):
        """
        load collision points for the arm and end effector
        """
        collision_pts = []
        links = [
            "link1",
            "link2",
            "link3",
            "link4",
            "link5",
            "link6",
            "link7",
            "hand",
            "finger",
            "finger",
        ]
        for i in range(len(links)):
            file = config.cfg.robot_model_path + "/{}.xyz".format(links[i])
            pts = np.loadtxt(file)[:, :3]
            sample_pts = pts[
                random.sample(range(pts.shape[0]), config.cfg.collision_point_num)
            ]
            collision_pts.append(sample_pts)
        return np.array(collision_pts)

    def resample_attached_object_collision_points(self, obj):
        """
        resample the collision points of end effector to include attaced object for placement
        """
        offset_pose = self.robot_kinematics.center_offset
        rel_pose = unpack_pose(obj.rel_hand_pose)
        hand_pose = np.linalg.inv(offset_pose[-3]).dot(rel_pose)

        f_pose = hand_pose  # self.robot_kinematics.finger_pose.dot
        lf_pose = np.linalg.inv(offset_pose[-2]).dot(f_pose)  # f_pose
        rf_pose = np.linalg.inv(offset_pose[-1]).dot(f_pose)  # f_pose

        hand_points = self.hand_col_points
        num = hand_points.shape[1]
        hand_pt_num = int(num / 4)
        hand_points = hand_points[0][random.sample(range(num), hand_pt_num)]
        attached_pt_num = num - hand_pt_num
        if hasattr(obj, 'points'):
            # print('add object points in hand collision avoidance')
            attached_hand_obj_points = obj.points[
                random.sample(range(200), attached_pt_num)
            ]
            attached_lf_obj_points = obj.points[random.sample(range(200, 350), num)]
            attached_rf_obj_points = obj.points[random.sample(range(350, 500), num)]

            attached_obj_hand_points = (
                np.matmul(hand_pose[:3, :3], attached_hand_obj_points[:, :3].T)
                + hand_pose[:3, [3]]
            ).T
            attached_obj_left_points = (
                np.matmul(lf_pose[:3, :3], attached_lf_obj_points[:, :3].T)
                + lf_pose[:3, [3]]
            ).T
            attached_obj_right_points = (
                np.matmul(rf_pose[:3, :3], attached_rf_obj_points[:, :3].T)
                + rf_pose[:3, [3]]
            ).T

            self.collision_points[-3] = np.concatenate(
                [hand_points, attached_obj_hand_points], axis=0
            )
            self.collision_points[-2] = attached_obj_left_points
            self.collision_points[-1] = attached_obj_right_points

    def reset_hand_points(self):
        """
        resset the collision points of end effector
        """
        self.collision_points[-3:] = self.hand_col_points.copy()


class Env(object):
    """
    Environment class
    """

    def __init__(self, cfg):
        self.robot = Robot(config.cfg.root_dir)
        self.config = config.cfg
        self.objects = []
        self.names = []
        self.indexes = []
        self.sdf_torch = None
        self.sdf_limits = None
        self.target_idx = 0

        if len(config.cfg.scene_file) > 0:
            full_path = config.cfg.scene_path + config.cfg.scene_file + ".mat"
            print('load from scene:', full_path)
            scene = sio.loadmat(full_path)
            poses = scene["pose"]
            path = scene["path"]
            self.objects_paths = [p.strip() + "/" for p in path]  # .encode("utf-8")

            self.objects.append(Model(self.objects_paths[0], 0, True, pose=poses[0]))
            self.names.append(self.objects[-1].name)

            for i in range(1, len(self.objects_paths)):
                self.objects.append(
                    Model(self.objects_paths[i], i, False, pose=poses[i])
                )
                self.names.append(self.objects[-1].name)

            self.indexes = list(range(len(self.names)))
            self.combine_sdfs()
            if 'target_name' in scene:
                self.set_target(scene['target_name'][0])

    def add_plane(self, trans, quat):
        """
        Add plane
        """
        pose = np.eye(4)
        pose[:3, 3] = trans  # fixed pose
        pose[:3, :3] = quat2mat(quat)
        plane_path = "data/objects/floor/"
        self.objects.append(
            Model(plane_path, len(self.objects), False, pose=pose, compute_grasp=False)
        )
        self.names.append(self.objects[-1].name)
        self.indexes.append(len(self.indexes))

    def add_table(self, trans, quat):
        """
        Add table
        """
        pose = np.eye(4)
        pose[:3, 3] = trans  # hopefully fixed pose
        pose[:3, :3] = quat2mat(quat)
        table_path = "data/objects/table/models/"
        self.objects.append(
            Model(table_path, len(self.objects), False, pose=pose, compute_grasp=False)
        )
        self.names.append(self.objects[-1].name)
        self.indexes.append(len(self.indexes))

    def add_object(
        self, name, trans, quat, insert=False, compute_grasp=True, model_name=None
    ):
        """
        Add an object
        """
        if model_name is None:
            model_name = name
        ycb_obj_path = os.path.join("data/objects", model_name + "/")
        pose = np.eye(4)
        pose[:3, 3] = trans
        pose[:3, :3] = quat2mat(quat)

        self.objects.append(
            Model(
                ycb_obj_path,
                len(self.objects),
                False,
                pose=pose,
                compute_grasp=compute_grasp,
                name=name,
            )
        )
        self.names.append(name)
        self.indexes.append(len(self.indexes))

    def set_target(self, name):
        """
        Set the grasping target
        """

        index = self.names.index(name)
        self.target_idx = index
        self.objects[self.target_idx].compute_grasp = True
        self.objects[self.target_idx].type = 'target'

    def remove_object(self, name, lazy=False):
        """
        Remove an object
        """
        index = self.names.index(name)
        for obj_list in [self.objects, self.names, self.indexes]:
            del obj_list[index]
        if not lazy:
            self.combine_sdfs()

    def clear(self):
        """
        Clean the scene
        """
        for name in self.names:
            self.remove_object(name, lazy=True)

    def update_pose(self, name, pose):
        index = self.names.index(name)
        self.objects[index].update_pose(pose)

    # combine sdfs of all the objects in the env
    def combine_sdfs(self):
        s = time.time()
        max_shape = np.array([obj.sdf.data.shape for obj in self.objects]).max(axis=0)
        if config.cfg.report_time:
            print("sdf max shape %d %d %d" % (max_shape[0], max_shape[1], max_shape[2]))
        num = len(self.objects)
        self.sdf_torch = torch.ones(
            (num, max_shape[0], max_shape[1], max_shape[2]), dtype=torch.float32 # float32
        ).cuda()
        self.sdf_limits = np.zeros((num, 10), dtype=np.float32)
        for i in range(num):
            obj = self.objects[i]
            size = obj.sdf.data.shape
            self.sdf_torch[i, : size[0], : size[1], : size[2]] = obj.sdf.data_torch
            xmins, ymins, zmins = obj.sdf.min_coords
            xmaxs, ymaxs, zmaxs = obj.sdf.max_coords
            self.sdf_limits[i, 0] = xmins
            self.sdf_limits[i, 1] = ymins
            self.sdf_limits[i, 2] = zmins
            self.sdf_limits[i, 3] = xmins + (xmaxs - xmins) * max_shape[0] / size[0]
            self.sdf_limits[i, 4] = ymins + (ymaxs - ymins) * max_shape[1] / size[1]
            self.sdf_limits[i, 5] = zmins + (zmaxs - zmins) * max_shape[2] / size[2]
            self.sdf_limits[i, 6] = max_shape[0]
            self.sdf_limits[i, 7] = max_shape[1]
            self.sdf_limits[i, 8] = max_shape[2]
            self.sdf_limits[i, 9] = obj.sdf.delta
            if config.cfg.report_time:
                print(
                    "%s, shape %d, %d, %d, sdf limit %f, %f, %f, %f, %f, %f, delta %f"
                    % (
                        obj.name,
                        size[0],
                        size[1],
                        size[2],
                        xmins,
                        ymins,
                        zmins,
                        xmaxs,
                        ymaxs,
                        zmaxs,
                        obj.sdf.delta,
                    )
                )
        if config.cfg.report_time:
            print("combine sdf time {:.3f}".format(time.time() - s))
        self.sdf_limits = torch.from_numpy(self.sdf_limits).cuda()

class PointEnv(Env):
    """
    Environment class for point cloud SDF
    """

    def __init__(self, cfg):
        super(PointEnv, self).__init__(cfg)
        self.env_points = None
        self.env_sdf = None
        self.grid_resolution = 0.02
        self.workspace_bounds = None
        self.workspace_points = None

    def compute_sdf_from_points(self, points, vis_points=None, vis=False):
        """
        Environment perceived points as N x 3 in the robot base coordinates
        we use nearest point distance to represent SDF for simplicity
        """
        self.env_points = points

        if points.shape[0] == 0:
            points = np.ones((2, 3)) * 3
        self.workspace_bounds = np.stack((points.min(0), points.max(0)), axis=1)
        margin = 0.24
        self.workspace_points = np.array(np.meshgrid(
                                np.arange(self.workspace_bounds[0][0] - margin, self.workspace_bounds[0][1] + margin, self.grid_resolution),
                                np.arange(self.workspace_bounds[1][0] - margin, self.workspace_bounds[1][1] + margin, self.grid_resolution),
                                np.arange(self.workspace_bounds[2][0] - margin, self.workspace_bounds[2][1] + margin, self.grid_resolution),
                                indexing='ij'))
        print('computing SDF from point cloud ...', self.workspace_bounds, self.workspace_points.shape)
        self.add_object('perception/env_points', [0,0,0], [1,0,0,0], insert=False, compute_grasp=False, model_name=None )

        # set sdf manually
        workspace_points_flattened = self.workspace_points.reshape((3, -1)).T
        s = time.time()
        self.kdtree = cKDTree(points)
        dists, closest_idx = self.kdtree.query(workspace_points_flattened)

        print('process point SDF time:', time.time() - s)
        self.objects[0].sdf = SignedDensityField(dists.reshape(self.workspace_points.shape[1:]), self.workspace_bounds[:,0] - margin, self.grid_resolution)
        self.objects[0].extents = self.workspace_bounds[0] - self.workspace_bounds[1]
        self.objects[0].resize = 1.
        self.objects[0].vis_points = vis_points.T if vis_points is not None else points.T
        print('vis point shape:', self.objects[0].vis_points.shape)
        self.combine_sdfs()

class PlanningScene(object):
    """
    Environment class
    """

    def __init__(self, cfg):
        self.traj = Trajectory(config.cfg.timesteps)
        print("Setting up env...")
        start_time = time.time()
        self.env = Env(config.cfg) if not cfg.use_point_sdf else PointEnv(config.cfg)
        print("env init time: {:.3f}".format(time.time() - start_time))
        config.cfg.ROBOT = self.env.robot.robot_kinematics  # hack for parallel ik
        if len(config.cfg.scene_file) > 0:
            self.planner = Planner(self.env, self.traj, lazy=config.cfg.default_lazy)
            if config.cfg.vis:
                self.setup_renderer()

    def update_planner(self, scene_idx=None):
        self.planner.update(self.env, self.traj, scene_idx=scene_idx)

    def reset(self, lazy=False, scene_idx=None):
        """
        Reset the scene for next run
        """
        self.planner = Planner(self.env, self.traj, lazy, scene_idx=scene_idx)
        if config.cfg.vis and not hasattr(self, "renderer"):
            self.setup_renderer()

    def fast_debug_vis(
        self,
        traj=None,
        interact=1,
        collision_pt=False,
        traj_type=2,
        nonstop=True,
        write_video=False,
        goal_set=False,
    ):
        """
        Debug and trajectory and related information
        """
        def fast_vis_simple(poses, cls_indexes, obj_vis_points, interact):

            vis_pt = []
            vis_color = []
            point_size = []
            visualize_context={"white_bg": True}

            if len(obj_vis_points) > 0 and len(obj_vis_points[0]) > 0:

                points = obj_vis_points[0]
                if points.shape[0] == 4:
                    label = points[3, :]

                    # target
                    target = points[:3, label == 0]
                    vis_pt.extend([target])
                    vis_color.append([0, 255, 0])
                    point_size.append(3)

                    # other
                    other = points[:3, label == 1]
                    vis_pt.extend([other])
                    vis_color.append([255, 0, 0])
                    point_size.append(3)
                else:
                    vis_pt.extend([points])
                    vis_color.append([255, 0, 0])
                    point_size.append(3)

                visualize_context = { "white_bg": True, "project_point":vis_pt,
                                    "project_color":vis_color,
                                    "reset_line_point": True }
                if  hasattr(config.cfg, 'external_grasps') and config.cfg.external_grasps is not None:
                    line_starts, line_ends = grasp_gripper_lines(config.cfg.external_grasps)
                    visualize_context["line"] = [(line_starts[0], line_ends[0])]
                    visualize_context["line_color"] = [[0, 0, 255]]

            return self.renderer.vis(
                            poses,
                            cls_indexes,
                            interact=interact,
                            visualize_context=visualize_context,
                            cam_pos=self.cam_pos,
                            V=self.cam_V,
                            shifted_pose=np.eye(4),
                        )

        def fast_vis_end(poses, cls_indexes, nonstop, obj_vis_points):
            visualize_context={"white_bg": True}
            if len(obj_vis_points) > 0:
                visualize_context["project_point"] = obj_vis_points
            return  self.renderer.vis(
                    poses,
                    cls_indexes,
                    interact=2 - int(nonstop),
                    cam_pos=self.cam_pos,
                    V=self.cam_V,
                    shifted_pose=np.eye(4),
                    visualize_context=visualize_context,
                )

        def fast_vis_collision(poses, cls_indexes, i, collision_pts, obj_vis_points, interact):
            vis_pt = [collision_pts[i-1,:,:,:3].reshape(-1, 3).T]  #-1
            vis_color = [collision_pts[i-1,:,:,6:9].reshape(-1, 3).astype(np.int)] #
            point_size = [3]
            if len(obj_vis_points) > 0 and len(obj_vis_points[0]) > 0:
                vis_pt.extend(obj_vis_points)
                vis_color.append([255, 0, 0])
                point_size.append(3)

            vis_pt_with_grad = -collision_pts[i-1,:,:,9:12].reshape(-1, 3).T * 0.4 + vis_pt[0] #
            return self.renderer.vis(
                            poses,
                            cls_indexes,
                            interact=interact,
                            visualize_context={"line":[(vis_pt_with_grad, vis_pt[0])],
                                "project_point":vis_pt, "project_color":vis_color,
                                "reset_line_point": True,
                                "line_color":[[0,255,255]], "point_size": point_size, "white_bg": True},
                            cam_pos=self.cam_pos,
                            V=self.cam_V,
                            shifted_pose=np.eye(4),
                        )

        def fast_vis_goalset(poses, cls_indexes, i, traj, interact):
            optim_i = int(float(i) / traj.shape[0] * config.cfg.optim_steps)
            vis_goal_set = optim_i < len(self.planner.selected_goals)
            mix_frame_image = self.renderer.vis(
                            poses,
                            cls_indexes,
                            interact=interact if not vis_goal_set else 0,
                            visualize_context={ "white_bg": True},
                            cam_pos=self.cam_pos,
                            V=self.cam_V,
                            shifted_pose=np.eye(4),
                        )

            if vis_goal_set:
                goal = self.planner.selected_goals[optim_i]
                goal_poses, goal_idx = get_sample_goals(self, self.traj.goal_set, goal)
                bg_image = self.renderer.vis(
                            goal_poses[:-3],
                            goal_idx[:-3],
                            interact=0,
                            visualize_context={ "white_bg": True},
                            cam_pos=self.cam_pos,
                            V=self.cam_V,
                            shifted_pose=np.eye(4),
                        ).astype(np.float32)
                bg_mask = (bg_image == 255).sum(-1) != 3
                bg_image[:,:,[1, 2]] *= 0.1

                bg_image2 = self.renderer.vis(
                            goal_poses[-3:],
                            goal_idx[-3:],
                            interact=0,
                            visualize_context={ "white_bg": True},
                            cam_pos=self.cam_pos,
                            V=self.cam_V,
                            shifted_pose=np.eye(4),
                        ).astype(np.float32)
                bg_mask2 = (bg_image2 == 255).sum(-1) != 3
                bg_image2[:,:,[0, 2]] *= 0.1 # green
                bg_image[bg_mask2] = bg_image2[bg_mask2]
                bg_mask = bg_mask | bg_mask2
                mix_frame_image[bg_mask] = bg_image[bg_mask] * 0.5 + mix_frame_image[bg_mask] * 0.5
                mix_frame_image = mix_frame_image.astype(np.uint8)
                if interact >= 1:
                    cv2.imshow('test', mix_frame_image[:,:,[2,1,0]])
                    cv2.waitKey(1)
            return mix_frame_image

        def fast_debug_traj(traj):
            frames = []
            for i in range(traj.shape[0]):
                cls_indexes, poses = self.prepare_render_list(traj[i])
                # get points rendering
                obj_vis_points = [obj.vis_points for obj in self.env.objects if obj.vis_points is not None]

                text = "OMG" if i < config.cfg.timesteps else "standoff"
                if collision_pt and i > 0 and i < config.cfg.timesteps:
                    frames.append(
                        fast_vis_collision(poses, cls_indexes, i, collision_pts, obj_vis_points, interact)
                    )

                elif goal_set and i > 0:
                    frames.append(
                        fast_vis_goalset(poses, cls_indexes, i, traj, interact)
                    )
                else:
                    frames.append(
                        fast_vis_simple(poses, cls_indexes, obj_vis_points, interact)
                    )

            if interact > 0:
                fast_vis_end(poses, cls_indexes, nonstop, obj_vis_points)
            return frames

        if traj is None and len(self.planner.history_trajectories) > 0:
            traj = self.planner.history_trajectories[-1]
        if len(self.planner.info) > 0:
            collision_pts = self.planner.info[-1]["collision_pts"]
        if traj_type == 0:
            traj = self.traj.start[None, :]
        elif traj_type == 1:
            collision_pts = collision_pts[-len(traj) :]
        elif traj_type == 2:
            traj = np.concatenate([self.traj.start[None, :], traj], axis=0)
        elif traj_type == 3:
            traj = self.traj.end[None, :]
        frames = fast_debug_traj(traj)
        if write_video:
            if not hasattr(self, "video_writer"):
                self.video_writer = config.make_video_writer(
                    config.cfg.output_video_name
                )
            print("video would be save to:", config.cfg.output_video_name)
            for frame in frames:
                self.video_writer.write(frame[..., [2, 1, 0]])

    def insert_object(self, name, trans, quat, model_name=None):
        """
        Wrapper to work with renderer (only be called for object changes)
        """
        if model_name is None:
            model_name = name
        self.env.add_object(name, trans, quat, insert=True, model_name=model_name)
        self.env.combine_sdfs()
        models = [self.env.objects[-1].mesh_path]
        textures = ["" for _ in models]
        colors = [[255, 0, 0]]
        resize = [self.env.objects[-1].resize]
        self.renderer.load_objects(models, scale=resize, add=True)

    def step(self):
        """
        Run an optimization step
        """
        plan = self.planner.plan(self.traj)
        return plan

    def prepare_render_list(self, joints):
        """
        Prepare the render poses for robot arm and objects
        """
        r = self.env.robot.robot_kinematics
        joints = wrap_value(joints)
        robot_poses = r.forward_kinematics_parallel(
            np.array(joints)[None, ...], base_link=config.cfg.base_link, offset=config.cfg.robot_vis_offset
        )[0]
        cls_indexes = list(range(10))
        poses = [pack_pose(pose) for pose in robot_poses]
        base_idx = 10
        cls_indexes += [idx + base_idx for idx in self.env.indexes if self.env.objects[idx].vis_points is None]
        object_pose = [obj.pose for obj in self.env.objects if obj.vis_points is None]
        if len(self.env.objects) > 0 and self.env.objects[self.env.target_idx].attached:
            object_pose[self.env.target_idx] = compose_pose(
                poses[7], self.env.objects[self.env.target_idx].rel_hand_pose )
        poses = poses + object_pose
        return cls_indexes, poses

    def setup_renderer(self):
        """
        Set up default parameters for the renderer.
        """
        print("Setting up renderer...")
        start_time = time.time()
        width = config.cfg.window_width
        height = config.cfg.window_height
        cam_param = [
            width,
            height,
            525.0 * width / 640,
            525.0 * height / 480,
            width / 2,
            height / 2,
            0.01,
            6,
        ]
        renderer = YCBRenderer(width=int(width), height=int(height))

        print("Renderer init time: {:.3f}".format(time.time() - start_time))
        start_time = time.time()
        """ robot """
        links = [
            "link1",
            "link2",
            "link3",
            "link4",
            "link5",
            "link6",
            "link7",
            "hand",
            "finger",
            "finger",
        ]
        models = [
            config.cfg.robot_model_path + "/{}.DAE".format(item) for item in links
        ]
        colors = [[0.1 * (idx + 1), 0, 0] for idx in range(len(links))]
        textures = ["" for _ in links]
        resize = [1.0 for _ in links]

        """ objects """
        models += [obj.mesh_path for obj in self.env.objects if os.path.exists(obj.mesh_path)]
        textures += ["" for _ in models]
        colors += [[255, 0, 0] for obj in self.env.objects]
        resize += [obj.resize for obj in self.env.objects]
        renderer.load_objects(models, scale=resize)

        """ renderer """
        renderer.set_projection_matrix(*cam_param)
        renderer.set_camera_default()
        renderer.set_light_pos(np.random.uniform(-0.1, 0.1, 3))
        renderer.set_light_pos(np.ones(3) * 1.2)
        self.renderer = renderer

        self.cam_pos = config.cfg.cam_pos
        self.cam_V = config.cfg.cam_V
        print("Renderer loading time: {:.3f}".format(time.time() - start_time))


if __name__ == "__main__":

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--vis", help="visualization", action="store_true"
    )
    parser.add_argument(
        "-vc", "--vis_collision_pt", help="collision visualization", action="store_true"
    )
    parser.add_argument(
        "-vg", "--vis_goalset", help="goalset visualization", action="store_true"
    )

    parser.add_argument("-f", "--file", help="filename", type=str, default="demo_scene_1")
    parser.add_argument("-w", "--write_video", help="write video", action="store_true")
    parser.add_argument("-g", "--grasp", help="grasp initialization", type=str, default="grasp")
    parser.add_argument("-exp", "--experiment", help="loop through the 100 scenes", action="store_true")
    parser.add_argument("-p", "--perception", help="use pointcloud and known grasp set as input", action="store_true")

    args = parser.parse_args()
    config.cfg.output_video_name = "output_videos/" + args.file + ".avi"
    mkdir_if_missing('output_videos')
    config.cfg.scene_file = args.file
    config.cfg.cam_V = np.array(
        [
            [-0.9351, 0.3518, 0.0428, 0.3037],
            [0.2065, 0.639, -0.741, 0.132],
            [-0.2881, -0.684, -0.6702, 1.8803],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    config.cfg.traj_init = args.grasp
    config.cfg.vis = args.vis or args.write_video

    if not args.experiment:
        if not args.perception:
            # example for standard planning: pose estimation with known models
            scene = PlanningScene(config.cfg)
            info = scene.step()
            if args.vis or args.write_video:
                scene.fast_debug_vis(interact=int(args.vis), write_video=args.write_video,
                            nonstop=True, collision_pt=args.vis_collision_pt, goal_set=args.vis_goalset)
        else:
            # example for planning from observation:
            # require target segmentation and grasps
            # approximate obstacles with point clouds

            # load the standard scene, render and get observation point clouds and segmentations
            scene = PlanningScene(config.cfg)
            scene.fast_debug_vis(interact=0, traj_type=0)
            frames = scene.renderer.rendered_frames
            mask = frames[1]
            points = frames[3][...,:3]
            robot_mesh_num = 10
            target_mask = close_integer(mask[...,-2], scene.env.target_idx + robot_mesh_num)
            bg_mask = close_integer(mask.sum(-1), len(scene.renderer.objects) - 1)
            non_target_mask = (~bg_mask) * (~target_mask) * (mask[...,-2] > robot_mesh_num)

            # cv2.imshow('test1',  target_mask.astype(np.uint8) * 255)
            # cv2.imshow('test2',  non_target_mask.astype(np.uint8) * 255)
            # cv2.waitKey(0)

            # use loaded grasps as external sources of goals
            grasps = sio.loadmat(os.path.join('data/scenes', args.file))["grasp_iks"]
            config.cfg.external_grasps = scene.env.robot.robot_kinematics.forward_kinematics_parallel(wrap_values(grasps))[:,7]
            config.cfg.use_external_grasp = True
            config.cfg.use_point_sdf = True
            config.cfg.scene_file = ''
            target_pt = points[target_mask][:,:3]
            nontarget_pt = points[non_target_mask][:,:3]
            nontarget_pt = scene.renderer.V[:3, :3].T.dot(nontarget_pt.T - scene.renderer.V[:3, [3]]).T  # base coordinate
            target_pt = scene.renderer.V[:3, :3].T.dot(target_pt.T - scene.renderer.V[:3, [3]]).T  # base coordinate
            target_pt = target_pt[np.random.choice(len(target_pt), 1024)]
            nontarget_pt = nontarget_pt[np.random.choice(len(nontarget_pt), 3072)]
            point_state = np.concatenate((target_pt, nontarget_pt), axis=0)
            point_mask = np.concatenate((np.zeros((len(target_pt), 1)), np.ones((len(nontarget_pt), 1))), axis=0)
            point_state = np.concatenate((point_state, point_mask), axis=-1)

            scene = PlanningScene(config.cfg)
            scene.env.clear()
            scene.env.compute_sdf_from_points(nontarget_pt, point_state )
            scene.reset(lazy=True)
            info = scene.step()
            scene.fast_debug_vis(interact=int(args.vis), write_video=args.write_video,
                            nonstop=True, collision_pt=args.vis_collision_pt, goal_set=args.vis_goalset)

    else:
        # loop through experiment scenes
        scene_files = ['scene_{}'.format(i) for i in range(100)]
        for scene_file in scene_files:
            config.cfg.output_video_name = "output_videos/" + scene_file
            config.cfg.output_video_name = config.cfg.output_video_name + ".avi"
            config.cfg.scene_file = scene_file
            config.cfg.use_standoff = False

            scene = PlanningScene(config.cfg)
            info = scene.step()

            if args.vis or args.write_video:
                scene.fast_debug_vis(interact=int(args.vis), write_video=args.write_video, nonstop=True,
                                     collision_pt=args.vis_collision_pt, goal_set=args.vis_goalset)
                if args.vis:
                    scene.renderer.release()
