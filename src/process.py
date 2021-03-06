import os
import numpy as np
import torch
from scipy import ndimage
from torch.utils.data import DataLoader
from .dataset import Dataset
from .models import InpaintingModel
from .utils import Progbar, create_dir, stitch_images, imsave
from .metrics import PSNR, SSIM
from libs.logger import Logger
import matplotlib.pyplot as plt
import time

# cross_level Features Net
class CLFNet():
    def __init__(self, config):
        self.config = config
        model_name = 'inpaint'
        self.debug = False
        self.model_name = model_name
        self.inpaint_model = InpaintingModel(config).to(config.DEVICE)

        # summary(InpaintingModel, (3, 256, 256), 6)
        # print(InpaintingModel)
        self.psnr = PSNR(255.0).to(config.DEVICE)
        self.ssim = SSIM(window_size=11)

        val_sample = int(float((self.config.EVAL_INTERVAL)))
        # test mode
        if self.config.MODE == 2 or self.config.MODE == 4 or self.config.MODE == 5:
            self.test_dataset = Dataset(config, config.TEST_FLIST, config.TEST_MASK_FLIST,
                                        augment=False, training=False)
        else:
            self.train_dataset = Dataset(config, config.TRAIN_FLIST, config.TRAIN_MASK_FLIST,
                                         augment=True, training=True)
            self.val_dataset = Dataset(config, config.VAL_FLIST, config.VAL_MASK_FLIST,
                                       augment=False, training=True, sample_interval=val_sample)
            self.sample_iterator = self.val_dataset.create_iterator(config.SAMPLE_SIZE)

        self.samples_path = os.path.join(config.PATH, 'samples')
        self.results_path = os.path.join(config.PATH, 'results')

        if config.RESULTS is not None:
            self.results_path = os.path.join(config.RESULTS)

        if config.DEBUG is not None and config.DEBUG != 0:
            self.debug = True

        # self.log_file = os.path.join(config.PATH, 'log_' + model_name + '.dat')
        log_path = os.path.join(config.PATH, 'logs_' + model_name)
        create_dir(log_path)
        self.logger = Logger(log_path)

    def load(self):
        self.inpaint_model.load()

    def save(self, epoch):
        self.inpaint_model.save(epoch)

    def train(self):
        train_loader = DataLoader(
            dataset=self.train_dataset,
            batch_size=self.config.BATCH_SIZE,
            num_workers=4,
            drop_last=True,
            shuffle=True
        )

        keep_training = True
        model = self.config.MODEL
        # max_iteration = int(float((self.config.MAX_ITERS)))
        step_per_epoch = int(float((self.config.MAX_STEPS)))
        max_epoches = int(float((self.config.MAX_EPOCHES)))
        total = int(len(self.train_dataset))

        if total == 0:
            print('No training data was provided! Check \'TRAIN_FLIST\' value in the configuration file.')
            return

        print('\nThe number of Training data is %d' % total)

        epoch = self.inpaint_model.epoch + 1 if self.inpaint_model.epoch != None else 1

        print('\nTraining epoch: %d' % epoch)
        progbar = Progbar(step_per_epoch, width=30, stateful_metrics=['step'])
        logs_ave = {}
        train_times = 0
        while (keep_training):
            for items in train_loader:
                self.inpaint_model.train()
                images, images_gray, masks = self.cuda(*items)
                # edge model
                # inpaint model

                # train
                outputs, gen_loss, dis_loss, logs = self.inpaint_model.process(images, masks)
                outputs_merged = (outputs * (1 - masks)) + (images * (masks))

                # metrics
                psnr = self.psnr(self.postprocess(images), self.postprocess(outputs_merged))
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / torch.sum(images)).float()
                logs['psnr'] = psnr.item()
                # logs['mae'] = mae.item()

                # backward
                self.inpaint_model.backward(gen_loss, dis_loss)
                if self.inpaint_model.iteration > step_per_epoch:
                    self.inpaint_model.iteration = 0
                    iteration = 0
                iteration = self.inpaint_model.iteration

                if iteration == 1:  # first step in this epoch
                    for tag, value in logs.items():
                        logs_ave[tag] = value
                else:
                    for tag, value in logs.items():
                        logs_ave[tag] += value
                if iteration == 0:  # mean to jump to new epoch

                    self.sample(epoch)
                    self.eval(epoch)
                    self.save(epoch)

                    # log current epoch in tensorboard
                    for tag, value in logs_ave.items():
                        self.logger.scalar_summary(tag, value / step_per_epoch, epoch)

                    # if reach max epoch
                    if epoch >= max_epoches:
                        keep_training = False
                        break
                    epoch += 1
                    # new epoch
                    print('\n\nTraining epoch: %d' % epoch)
                    for tag, value in logs.items():
                        logs_ave[tag] = value
                    progbar = Progbar(step_per_epoch, width=30, stateful_metrics=['step'])
                    self.inpaint_model.iteration += 1  # jump to new epoch and set the iteration to 1
                    iteration += 1
                logs['step'] = iteration
                progbar.add(1,
                            values=logs.items() if self.config.VERBOSE else [x for x in logs.items() if
                                                                             not x[0].startswith('l_')])
            train_times += 1
            print("The whole data hase been trained %d times" % train_times)

        print('\nEnd training....\n')

    def eval(self, epoch):
        self.val_loader = DataLoader(
            dataset=self.val_dataset,
            batch_size=self.config.BATCH_SIZE,
            drop_last=True,
            shuffle=False,
            num_workers=4
        )
        model = self.config.MODEL
        total = int(len(self.val_dataset))

        self.inpaint_model.eval()

        progbar = Progbar(int(total / self.config.BATCH_SIZE), width=30, stateful_metrics=['step'])
        iteration = 0
        logs_ave = {}
        with torch.no_grad():
            for items in self.val_loader:
                iteration += 1
                images, images_gray, masks = self.cuda(*items)
                # inpaint model
                # eval
                outputs, gen_loss, dis_loss, logs = self.inpaint_model.process(images, masks)
                outputs_merged = (outputs * (1 - masks)) + (images * (masks))

                # metrics
                psnr = self.psnr(self.postprocess(images), self.postprocess(outputs_merged))
                mae = (torch.sum(torch.abs(images - outputs_merged)) / torch.sum(images)).float()
                logs['val_psnr'] = psnr.item()
                logs['val_mae'] = mae.item()
                # joint model
                if iteration == 1:  # first iteration
                    logs_ave = {}
                    for tag, value in logs.items():
                        logs_ave[tag] = value
                else:
                    for tag, value in logs.items():
                        logs_ave[tag] += value

                logs["step"] = iteration
                progbar.add(1, values=logs.items())

            for tag, value in logs_ave.items():
                self.logger.scalar_summary(tag, value / iteration, epoch)
            self.inpaint_model.iteration = 0

    def test(self):
        self.inpaint_model.eval()
        damaged_dir = os.path.join(self.results_path, "damaged")
        create_dir(damaged_dir)
        mask_dir = os.path.join(self.results_path, "mask")
        create_dir(mask_dir)
        inpainted_dir = os.path.join(self.results_path, "inpainted")
        create_dir(inpainted_dir)
        raw_dir = os.path.join(self.results_path, "raw")
        create_dir(raw_dir)

        create_dir(self.results_path)
        sample_interval = self.config.TEST_INTERVAL
        batch_size = 1
        test_loader = DataLoader(
            dataset=self.test_dataset,
            batch_size=batch_size,
            num_workers=1,
            shuffle=False
        )

        total = int(len(self.test_dataset)/ batch_size / sample_interval)
        progbar = Progbar(total, width=30, stateful_metrics=['step'])
        index = 0
        proc_start_time = time.time()
        total_time=0.

        with torch.no_grad():
            for items in test_loader:
                name = self.test_dataset.load_name(index)
                images, images_gray, masks = self.cuda(*items)
                index += 1
                if index >= total:
                    break;
                # Save raw
                # if self.config.SAVEIMG == 1:
                #     path = os.path.join(damaged_dir, name)
                #     damaged_img = self.postprocess(images * masks + (1 - masks))[0]
                #     imsave(damaged_img, path)
                #     # Save masks
                #     path = os.path.join(mask_dir, name)
                #     imsave(self.postprocess(masks), os.path.splitext(path)[0] + '.png')
                #     # Save Ground Truth
                #     path = os.path.join(raw_dir, name)
                #     img = self.postprocess(images)[0]
                #     imsave(img, path)
                #     # print(index, name)


                logs = {}
                # run model
                outputs = self.inpaint_model(images, masks)
                outputs_merged = (outputs * (1 - masks)) + (images * masks)

                output = self.postprocess(outputs_merged)[0]
                if self.config.SAVEIMG == 1:
                    path = os.path.join(inpainted_dir, name)
                    # print(index, name)
                    imsave(output, path)
                cnt_time = time.time() - proc_start_time
                total_time=total_time+cnt_time
                # print('Image {} done, time {}, totaltime{}, {} sec/Image'.format(index, cnt_time,total_time,
                #                                                             float(total_time) / index))

                psnr = self.psnr(self.postprocess(images), self.postprocess(outputs_merged))
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / torch.sum(images)).float()
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / images.numel()).float() = = L1 distance
                l1 = torch.nn.L1Loss()(images, outputs_merged)
                one_ssim = self.ssim(images, outputs_merged)
                logs["psnr"] = psnr.item()
                # logs["mae"] = mae.item()
                logs["L1"] = l1.item()
                logs["ssim"] = one_ssim.item()
                logs["step"] = index
                progbar.add(1, values=logs.items())

                if self.debug:
                    pass
                proc_start_time = time.time()

        print('\nEnd test....')
        print('Image {} done, time {}, average {} sec/Image'.format(total, total_time,
                                                                    float(total_time) / total))

    def progressive_test(self):
        self.inpaint_model.eval()
        damaged_dir = os.path.join(self.results_path, "damaged")
        create_dir(damaged_dir)
        mask_dir = os.path.join(self.results_path, "mask")
        create_dir(mask_dir)
        inpainted_dir = os.path.join(self.results_path, "inpainted")
        create_dir(inpainted_dir)
        raw_dir = os.path.join(self.results_path, "raw")
        create_dir(raw_dir)

        create_dir(self.results_path)
        sample_interval = self.config.TEST_INTERVAL
        batch_size = 1
        test_loader = DataLoader(
            dataset=self.test_dataset,
            batch_size=batch_size,
            num_workers=1,
            shuffle=True
        )

        total = int(len(self.test_dataset)/ batch_size / sample_interval)
        progbar = Progbar(total, width=30, stateful_metrics=['step'])
        index = 0
        total_time=0.
        proc_start_time = time.time()

        with torch.no_grad():
            for items in test_loader:
                logs = {}
                name = self.test_dataset.load_name(index)
                images, images_gray, masks = self.cuda(*items)
                index += 1
                if index > total:
                    break;
                # Save damaged image
                if self.config.SAVEIMG == 1:
                    path = os.path.join(damaged_dir, str(index)+'.jpg')
                    damaged_img = self.postprocess(images * masks + (1 - masks))[0]
                    imsave(damaged_img, path)
                    # Save masks
                    path = os.path.join(mask_dir, str(index))
                    imsave(self.postprocess(masks), os.path.splitext(path)[0] + '.png')
                    # Save Ground Truth
                    path = os.path.join(raw_dir, str(index)+'.jpg')
                    img = self.postprocess(images)[0]
                    imsave(img, path)
                    # print(index, name)

                # run model
                outputs = self.inpaint_model(images, masks)
                outputs_merged = (outputs * (1 - masks)) + (images * masks)

                # # Test again:
                # masks=masks.cpu().numpy().astype(np.uint8)   # [0-255]
                # # masks = (masks > 128).astype(np.uint8)                # [0-1]
                # struct = ndimage.generate_binary_structure(4, 5)
                # np_masks=ndimage.binary_dilation(masks,structure=struct).astype(masks.dtype)  # [0-1]
                # masks=torch.from_numpy(np_masks)
                np_masks = masks.cpu().numpy()
                NoHoleNum = np.count_nonzero(np_masks)
                ratio = (np_masks.size - NoHoleNum) / np_masks.size
                i = 1
                while ratio > 0.2 and i <= 10:
                    # Save masks again
                    # Erosion
                    masks = masks.cpu().numpy().astype(np.uint8)  # [0-255]
                    # np_masks = ndimage.grey_dilation(masks, size=(1, 1, 9, 9))
                    np_masks = ndimage.grey_dilation(masks, size=(1, 1, 15, 15))
                    masks = torch.from_numpy(np_masks).float().to(self.config.DEVICE)

                    if self.config.SAVEIMG == 1:
                        path = os.path.join(mask_dir, name)
                        imsave(self.postprocess(masks), os.path.splitext(path)[0] + '_%d.png' % i)
                    outputs = self.inpaint_model(outputs_merged, masks)
                    outputs_merged = (outputs * (1 - masks)) + (outputs_merged * masks)
                    i += 1

                    # count no hole number and ratio
                    NoHoleNum = np.count_nonzero(np_masks)
                    ratio = (np_masks.size - NoHoleNum) / np_masks.size

                if self.config.SAVEIMG == 1:
                    path = os.path.join(inpainted_dir, str(index)+'.jpg')
                    # print(index, name)
                    output = self.postprocess(outputs_merged)[0]
                    imsave(output, path)
                cnt_time = time.time() - proc_start_time
                total_time = total_time + cnt_time

                psnr = self.psnr(self.postprocess(images), self.postprocess(outputs_merged))
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / torch.sum(images)).float()
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / images.numel()).float()
                l1 = torch.nn.L1Loss()(images, outputs_merged)
                one_ssim = self.ssim(images, outputs_merged)
                logs["psnr"] = psnr.item()
                # logs["mae"] = mae.item()
                logs["L1"] = l1.item()
                logs["ssim"] = one_ssim.item()
                logs["step"] = index
                progbar.add(1, values=logs.items())

                if self.debug:
                    pass
                proc_start_time = time.time()

        print('\nEnd test....')
        print('Image {} done, time {}, average {} sec/Image'.format(total, total_time,
                                                                    float(total_time) / total))

    def feature_visualize(self,module, input):
        xs=input[0]
        index=0
        for x in xs:
            x=x[0]   # remove batch dimension
            min_num = np.minimum(16, x.size()[0])
            for i in range(min_num):
                plt.subplot(4, 4, i + 1)
                plt.axis('off')
                plt.imshow(x[i].cpu())

            name='stage_'+str(module.stage)+'branch_'+str(index)
            for i in x.shape:
                name=name+'_'+str(i)
            plt.savefig(name+'.png',bbox_inches='tight')
            plt.show()
            index+=1


    def visualization_test(self):
        self.inpaint_model.eval()
        for  m in self.inpaint_model.generator.modules():
            if isinstance(m,TwoBranchModule):
                m.register_forward_pre_hook(self.feature_visualize)
            # if isinstance(m, torch.nn.Conv2d):
            #     m.register_forward_pre_hook(self.feature_visualize)

        damaged_dir = os.path.join(self.results_path, "damaged")
        create_dir(damaged_dir)
        mask_dir = os.path.join(self.results_path, "mask")
        create_dir(mask_dir)
        inpainted_dir = os.path.join(self.results_path, "inpainted")
        create_dir(inpainted_dir)
        raw_dir = os.path.join(self.results_path, "raw")
        create_dir(raw_dir)

        create_dir(self.results_path)
        sample_interval = self.config.TEST_INTERVAL
        batch_size = 1
        test_loader = DataLoader(
            dataset=self.test_dataset,
            batch_size=batch_size,
            num_workers=1,
            shuffle=False
        )

        total = int(len(self.test_dataset))
        progbar = Progbar(int(total / batch_size / sample_interval), width=30, stateful_metrics=['step'])

        index = 0
        with torch.no_grad():
            for items in test_loader:
                name = self.test_dataset.load_name(index)
                images, images_gray, masks = self.cuda(*items)

                # Save damaged image
                if self.config.SAVEIMG == 1:
                    path = os.path.join(damaged_dir, name)
                    damaged_img = self.postprocess(images * masks + (1 - masks))[0]
                    imsave(damaged_img, path)
                    # Save masks
                    path = os.path.join(mask_dir, name)
                    imsave(self.postprocess(masks), os.path.splitext(path)[0] + '.png')
                    # Save Ground Truth
                    path = os.path.join(raw_dir, name)
                    img = self.postprocess(images)[0]
                    imsave(img, path)
                    # print(index, name)

                index += 1
                if index > total / batch_size / sample_interval:
                    break;
                logs = {}
                # run model
                outputs = self.inpaint_model(images, masks)
                outputs_merged = (outputs * (1 - masks)) + (images * masks)

                output = self.postprocess(outputs_merged)[0]
                if self.config.SAVEIMG == 1:
                    path = os.path.join(inpainted_dir, name)
                    # print(index, name)
                    imsave(output, path)
                psnr = self.psnr(self.postprocess(images), self.postprocess(outputs_merged))
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / torch.sum(images)).float()
                # mae = (torch.sum(torch.abs(images - outputs_merged)) / images.numel()).float()
                l1 = torch.nn.L1Loss()(images, outputs_merged)
                one_ssim = self.ssim(images, outputs_merged)
                logs["psnr"] = psnr.item()
                # logs["mae"] = mae.item()
                logs["L1"] = l1.item()
                logs["ssim"] = one_ssim.item()
                logs["step"] = index
                progbar.add(1, values=logs.items())

                if self.debug:
                    pass
                break
        print('\nEnd test....')

    def sample(self, it=None):
        # do not sample when validation set is empty
        if len(self.val_dataset) == 0:
            return

        self.inpaint_model.eval()

        model = self.config.MODEL
        with torch.no_grad():
            items = next(self.sample_iterator)
            images, images_gray, masks = self.cuda(*items)
            # (batch,channels,weighth,length)
            iteration = self.inpaint_model.iteration
            inputs = (images * masks) + (1 - masks)
            outputs = self.inpaint_model(images, masks).detach()

            if it is not None:
                iteration = it

            image_per_row = 2
            if self.config.SAMPLE_SIZE <= 6:
                image_per_row = 1

            images = stitch_images(
                self.postprocess(images),
                # self.postprocess(edges),
                self.postprocess(inputs),
                self.postprocess(outputs),
                # self.postprocess(outputs_merged),
                img_per_row=image_per_row
            )

            path = os.path.join(self.samples_path, self.model_name)
            name = os.path.join(path, str(iteration).zfill(3) + ".png")
            create_dir(path)
            print('\nsaving sample ' + name)
            images.save(name)

    def log(self, logs):
        with open(self.log_file, 'a') as f:
            f.write('%s\n' % ' '.join([str(item[1]) for item in logs]))

    def cuda(self, *args):
        return (item.to(self.config.DEVICE) for item in args)

    def postprocess(self, img):
        # [0, 1] => [0, 255]
        img = img * 255.0
        img = img.permute(0, 2, 3, 1)
        return img.int()

    def preprocess(self, img):
        # [0, 255] => [0, 1]
        img = np.expand_dims(img, axis=0)
        img = np.expand_dims(img, axis=0)
        # img = img.astype(float) / 255.0
        return img

    def color_the_edge(self, img, edges, masks):
        img = img.expand(-1, 3, -1, -1)
        yellow_v = (torch.tensor([215. / 255., 87. / 255., 15. / 255.]).reshape(1, 3, 1, 1)).to(self.config.DEVICE)
        yellow = img * (1 - masks) * yellow_v
        img = yellow + (edges * masks)
        return img
